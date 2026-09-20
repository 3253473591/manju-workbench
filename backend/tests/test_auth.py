"""Identity：注册、登录、会话、退出。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import auth_header, login, register_user


def test_register_creates_user_role(client: TestClient) -> None:
    response = register_user(client, "newbie", display_name="新人")
    assert response.status_code == 201, response.text
    user = response.json()["user"]
    assert user["username"] == "newbie"
    assert user["displayName"] == "新人"
    # 开放注册，角色固定为 user
    assert user["role"] == "user"
    assert user["status"] == "active"
    # 任何响应都不返回密码或哈希
    assert "password" not in user
    assert "passwordHash" not in user


def test_register_rejects_duplicate_username(client: TestClient) -> None:
    assert register_user(client, "dup").status_code == 201
    response = register_user(client, "dup")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REGISTER_USERNAME_EXISTS"


def test_register_enforces_password_policy(client: TestClient) -> None:
    response = register_user(client, "shorty", password="123")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PASSWORD_POLICY_FAILED"


def test_login_returns_token_once_and_me_works(client: TestClient) -> None:
    register_user(client, "carol")
    response = client.post(
        "/api/v1/auth/login", json={"username": "carol", "password": "secret123"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sessionToken"]
    assert body["expiresAt"].endswith("Z")

    me = client.get("/api/v1/auth/me", headers=auth_header(body["sessionToken"]))
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "carol"


def test_login_rejects_wrong_password(client: TestClient) -> None:
    register_user(client, "dave")
    response = client.post(
        "/api/v1/auth/login", json={"username": "dave", "password": "wrong-pass"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID"


def test_me_requires_token(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_me_rejects_garbage_token(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me", headers=auth_header("not-a-real-token"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_logout_revokes_session(client: TestClient) -> None:
    register_user(client, "erin")
    token = login(client, "erin")
    assert client.post("/api/v1/auth/logout", headers=auth_header(token)).status_code == 204
    after = client.get("/api/v1/auth/me", headers=auth_header(token))
    assert after.status_code == 401


def test_disabled_account_cannot_create_session(client: TestClient, admin_token: str) -> None:
    register_user(client, "frank")
    users = client.get(
        "/api/v1/admin/users", headers=auth_header(admin_token), params={"keyword": "frank"}
    ).json()
    user_id = users["items"][0]["id"]

    patched = client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=auth_header(admin_token),
        json={"status": "disabled"},
    )
    assert patched.status_code == 200, patched.text

    response = client.post(
        "/api/v1/auth/login", json={"username": "frank", "password": "secret123"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"


def test_disabled_account_existing_session_is_rejected(
    client: TestClient, admin_token: str
) -> None:
    register_user(client, "grace")
    token = login(client, "grace")

    users = client.get(
        "/api/v1/admin/users", headers=auth_header(admin_token), params={"keyword": "grace"}
    ).json()
    user_id = users["items"][0]["id"]
    client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=auth_header(admin_token),
        json={"status": "disabled"},
    )

    # 停用会吊销已有会话，且每次鉴权都会重新检查账号状态
    assert client.get("/api/v1/auth/me", headers=auth_header(token)).status_code == 401


def test_password_is_not_stored_in_plain_text(client: TestClient) -> None:
    from sqlmodel import select

    from app.db.engine import session_scope
    from app.models.identity import User

    register_user(client, "hashcheck")
    with session_scope() as db:
        user = db.exec(select(User).where(User.username == "hashcheck")).one()
        assert user.password_hash != "secret123"
        assert user.password_hash.startswith("pbkdf2_sha256$")
        assert "secret123" not in user.password_hash


def test_session_token_is_stored_hashed_only(client: TestClient) -> None:
    from sqlmodel import select

    from app.core.security import hash_token
    from app.db.engine import session_scope
    from app.models.identity import Session as SessionModel

    register_user(client, "tokencheck")
    token = login(client, "tokencheck")
    with session_scope() as db:
        records = db.exec(select(SessionModel)).all()
        stored = {r.token_hash for r in records}
        assert token not in stored
        assert hash_token(token) in stored
