"""三角色权限矩阵。矩阵来源：docs/architecture/domain-boundaries.md §3.2。

权限必须在后端校验；这些用例就是「前端隐藏按钮不算数」的证据。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import auth_header

PROJECT_PAYLOAD = {"name": "权限测试项目", "description": ""}


def test_all_roles_can_read_projects(
    client: TestClient, user_token: str, manager_token: str, admin_token: str
) -> None:
    for token in (user_token, manager_token, admin_token):
        response = client.get("/api/v1/projects", headers=auth_header(token))
        assert response.status_code == 200, response.text


def test_anonymous_cannot_read_projects(client: TestClient) -> None:
    response = client.get("/api/v1/projects")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_user_and_admin_can_create_project(
    client: TestClient, user_token: str, admin_token: str
) -> None:
    assert (
        client.post(
            "/api/v1/projects", headers=auth_header(user_token), json=PROJECT_PAYLOAD
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/projects", headers=auth_header(admin_token), json=PROJECT_PAYLOAD
        ).status_code
        == 201
    )


def test_manager_cannot_write(client: TestClient, manager_token: str) -> None:
    """manager 的只读是严格的：所有写操作一律 403。"""
    created = client.post(
        "/api/v1/projects", headers=auth_header(manager_token), json=PROJECT_PAYLOAD
    )
    assert created.status_code == 403
    assert created.json()["error"]["code"] == "FORBIDDEN"


def test_manager_cannot_patch_or_delete_project(
    client: TestClient, user_token: str, manager_token: str
) -> None:
    project_id = client.post(
        "/api/v1/projects", headers=auth_header(user_token), json=PROJECT_PAYLOAD
    ).json()["project"]["id"]

    patched = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=auth_header(manager_token),
        json={"name": "改名"},
    )
    assert patched.status_code == 403

    deleted = client.delete(
        f"/api/v1/projects/{project_id}", headers=auth_header(manager_token)
    )
    assert deleted.status_code == 403

    # manager 仍然可以读
    assert (
        client.get(f"/api/v1/projects/{project_id}", headers=auth_header(manager_token))
        .status_code
        == 200
    )


def test_only_admin_reaches_admin_routes(
    client: TestClient, user_token: str, manager_token: str, admin_token: str
) -> None:
    assert (
        client.get("/api/v1/admin/users", headers=auth_header(user_token)).status_code == 403
    )
    assert (
        client.get("/api/v1/admin/users", headers=auth_header(manager_token)).status_code
        == 403
    )
    assert (
        client.get("/api/v1/admin/users", headers=auth_header(admin_token)).status_code == 200
    )


def test_role_change_revokes_existing_sessions(
    client: TestClient, user_token: str, admin_token: str
) -> None:
    users = client.get(
        "/api/v1/admin/users", headers=auth_header(admin_token), params={"keyword": "alice"}
    ).json()
    user_id = users["items"][0]["id"]

    promoted = client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=auth_header(admin_token),
        json={"role": "manager"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["user"]["role"] == "manager"

    # 旧会话已吊销
    assert client.get("/api/v1/auth/me", headers=auth_header(user_token)).status_code == 401


def test_last_admin_cannot_be_demoted_or_disabled(
    client: TestClient, admin_token: str
) -> None:
    users = client.get(
        "/api/v1/admin/users", headers=auth_header(admin_token), params={"keyword": "admin"}
    ).json()
    admin_id = users["items"][0]["id"]

    demoted = client.patch(
        f"/api/v1/admin/users/{admin_id}",
        headers=auth_header(admin_token),
        json={"role": "user"},
    )
    assert demoted.status_code == 409
    assert demoted.json()["error"]["code"] == "LAST_ADMIN_REQUIRED"

    disabled = client.patch(
        f"/api/v1/admin/users/{admin_id}",
        headers=auth_header(admin_token),
        json={"status": "disabled"},
    )
    assert disabled.status_code == 409


def test_last_admin_rule_allows_demotion_when_another_admin_exists(
    client: TestClient, admin_token: str, user_token: str
) -> None:
    users = client.get(
        "/api/v1/admin/users", headers=auth_header(admin_token), params={"keyword": "alice"}
    ).json()
    alice_id = users["items"][0]["id"]
    assert (
        client.patch(
            f"/api/v1/admin/users/{alice_id}",
            headers=auth_header(admin_token),
            json={"role": "admin"},
        ).status_code
        == 200
    )

    bootstrap_id = client.get(
        "/api/v1/admin/users", headers=auth_header(admin_token), params={"keyword": "admin"}
    ).json()["items"][0]["id"]
    assert (
        client.patch(
            f"/api/v1/admin/users/{bootstrap_id}",
            headers=auth_header(admin_token),
            json={"role": "user"},
        ).status_code
        == 200
    )
