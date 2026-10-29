"""API 契约测试：错误封装、权限判定、`If-Match`、请求守卫、分页、金额格式。

这些行为在 `02` / `03` 里都是逐条写死的，出错时前端会误判（把 422 当 404、把重放当 412），
所以每条都配一个用例。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju.api import auth as auth_api
from manju.config import COOKIE_NAME, Settings
from manju.models import Clip, Revision
from tests.conftest import login


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def test_error_envelope_and_request_id(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me", headers={"X-Request-Id": "req_fixed123"})
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "AUTH_REQUIRED"
    assert error["message"] == "请先登录"
    assert error["request_id"] == "req_fixed123"
    assert response.headers["X-Request-Id"] == "req_fixed123"


def test_unknown_route_and_method(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    missing = client.get("/api/v1/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    wrong_method = client.patch("/api/v1/auth/me", json={})
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_path_id_shape_vs_missing(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    assert client.get("/api/v1/clips/abc").status_code == 422
    assert client.get("/api/v1/clips/-1").status_code == 422
    assert client.get("/api/v1/clips/99999999999999999999").status_code == 422
    assert client.get("/api/v1/clips/999999").status_code == 404


def test_patch_requires_if_match_and_rejects_stale(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    clip_id = world["clip"].id
    url = f"/api/v1/clips/{clip_id}"

    missing = client.patch(url, json={"prompt": "改一句"})
    assert missing.status_code == 422
    assert missing.json()["error"]["detail"]["header"] == "If-Match"

    unquoted = client.patch(url, json={"prompt": "改一句"}, headers={"If-Match": "1"})
    assert unquoted.status_code == 422

    stale = client.patch(url, json={"prompt": "改一句"}, headers={"If-Match": '"99"'})
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "STALE_VERSION"

    ok = client.patch(url, json={"prompt": "改一句"}, headers={"If-Match": '"1"'})
    assert ok.status_code == 200
    assert ok.json()["version"] == 2
    assert ok.headers["ETag"] == '"2"'


def test_duration_boundary(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = f"/api/v1/episodes/{world['episode'].id}/clips"
    too_long = client.post(url, json={"duration_s": "15.01"})
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "VALIDATION_RANGE"
    ok = client.post(url, json={"duration_s": "15.00"})
    assert ok.status_code == 201
    assert ok.json()["duration_s"] == "15.00"


def test_admin_and_viewer_are_read_only(client: TestClient, world: dict, settings: Settings) -> None:
    clip_id = world["clip"].id
    url = f"/api/v1/clips/{clip_id}"

    login(client, world["admin"], settings)
    admin = client.patch(url, json={"prompt": "x"}, headers={"If-Match": '"1"'})
    assert admin.status_code == 403
    assert admin.json()["error"]["code"] == "PERM_READONLY_PROJECT"

    login(client, world["viewer"], settings)
    viewer = client.patch(url, json={"prompt": "x"}, headers={"If-Match": '"1"'})
    assert viewer.status_code == 403

    login(client, world["member"], settings)
    member = client.patch(url, json={"prompt": "x"}, headers={"If-Match": '"1"'})
    assert member.status_code == 200


def test_lead_cannot_create_project_when_user(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["member"], settings)
    denied = client.post("/api/v1/projects", json={"title": "新项目"})
    assert denied.status_code == 403

    login(client, world["owner"], settings)
    created = client.post("/api/v1/projects", json={"title": "新项目", "genre": "悬疑"})
    assert created.status_code == 201
    assert created.json()["owner_id"] == world["owner"].id
    assert created.json()["created_at"].endswith("Z")


def test_body_guards_content_type_and_size(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = "/api/v1/locks/acquire"
    payload = {"target_kind": "clip", "target_id": world["clip"].id}

    wrong_type = client.post(url, content=b'{"a":1}', headers={"Content-Type": "text/plain"})
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error"]["code"] == "UNSUPPORTED_MEDIA"

    huge = b"x" * (2 * 1024 * 1024 + 1)
    too_big = client.post(url, content=huge, headers={"Content-Type": "application/json"})
    assert too_big.status_code == 413

    assert client.post(url, json=payload).status_code == 200


def test_origin_guard(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    payload = {"target_kind": "clip", "target_id": world["clip"].id}
    allowed = client.post(
        "/api/v1/locks/acquire",
        json=payload,
        headers={"Origin": "http://localhost:5173"},
    )
    assert allowed.status_code == 200
    blocked = client.post(
        "/api/v1/locks/acquire",
        json=payload,
        headers={"Origin": "http://evil.example"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["detail"]["reason"] == "origin_not_allowed"


def test_login_rate_limited(client: TestClient, world: dict) -> None:
    auth_api._FAILURES.clear()
    body = {"email": "member@example.com", "password": "wrongpass123"}
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json=body).status_code == 401
    limited = client.post("/api/v1/auth/login", json=body)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert int(limited.headers["Retry-After"]) >= 1
    auth_api._FAILURES.clear()


def test_register_conflict_and_weak_password(client: TestClient) -> None:
    created = client.post("/api/v1/auth/register", json={"email": "dup@example.com", "password": "devpass12345"})
    assert created.status_code == 201
    assert created.json()["role"] == "user"
    assert client.cookies.get(COOKIE_NAME)

    again = client.post("/api/v1/auth/register", json={"email": "dup@example.com", "password": "devpass12345"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT_EMAIL"

    digits_only = client.post("/api/v1/auth/register", json={"email": "weak@example.com", "password": "1234567890"})
    assert digits_only.status_code == 422
    assert digits_only.json()["error"]["detail"]["field"] == "password"


def test_pagination_clamps_and_tolerates_bad_cursor(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    assert client.get("/api/v1/projects?limit=1000").status_code == 200
    bad_cursor = client.get("/api/v1/projects?cursor=0")
    assert bad_cursor.status_code == 200
    assert bad_cursor.json() == {"items": [], "next_cursor": None}
    assert client.get("/api/v1/projects?limit=abc").status_code == 422


def test_cost_money_is_string(client: TestClient, world: dict, settings: Settings, session: Session) -> None:
    session.add(
        Revision(
            target_kind="clip",
            target_id=world["clip"].id,
            model_requested="seedance-2.0-mini",
            model_used="seedance-2.0-mini",
            input_tokens=260_100,
            video_seconds=Decimal("12.00"),
            unit_price=Decimal("0.189167"),
            cost=Decimal("2.270000"),
            created_at=_now(),
        )
    )
    session.flush()
    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=model")
    assert response.status_code == 200
    body = response.json()
    assert body["total"]["cost"] == "2.270000"
    assert body["items"][0]["seconds"] == "12.00"
    assert body["items"][0]["key"] == "seedance-2.0-mini"

    bad_group = client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=stage")
    assert bad_group.status_code == 422


def test_lock_conflict_and_renew(client: TestClient, world: dict, settings: Settings) -> None:
    payload = {"target_kind": "clip", "target_id": world["clip"].id}
    login(client, world["owner"], settings)
    assert client.post("/api/v1/locks/acquire", json=payload).status_code == 200
    assert client.post("/api/v1/locks/acquire", json=payload).status_code == 200  # 同一人 = 续期

    login(client, world["member"], settings)
    conflict = client.post("/api/v1/locks/acquire", json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "LOCK_HELD"
    assert conflict.json()["error"]["detail"]["holder"] == "owner@example.com"

    assert client.post("/api/v1/locks/release", json=payload).status_code == 204
    # 不是持有者不能删别人的锁
    login(client, world["owner"], settings)
    assert client.post("/api/v1/locks/acquire", json=payload).status_code == 200


def test_delete_clip_with_replacement_blocked(
    client: TestClient, world: dict, settings: Settings, session: Session
) -> None:
    original = world["clip"]
    session.add(
        Clip(
            episode_id=original.episode_id,
            idx=original.idx,
            source_idx=original.source_idx,
            duration_s=Decimal("12.00"),
            status="draft",
            parent_clip_id=original.id,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    session.flush()
    login(client, world["owner"], settings)
    blocked = client.delete(f"/api/v1/clips/{original.id}")
    assert blocked.status_code == 422
    assert blocked.json()["error"]["detail"]["children"] == 1


def test_revisions_and_diff(client: TestClient, world: dict, settings: Settings, session: Session) -> None:
    clip_id = world["clip"].id
    for version, prompt in ((1, "镜头1（0-3秒）：远景\n"), (2, "镜头1（0-3秒）：远景急推\n")):
        session.add(
            Revision(
                target_kind="clip",
                target_id=clip_id,
                prompt=prompt,
                cost=Decimal("2.270000"),
                # params_json 里真有对象/整数/布尔（S4 写 prompt 对象、steps 写 skill_sections）——
                # 只用字符串参数测会在界面上"对比上一版"时 500
                params_json={
                    "version": f"v{version}",
                    "res": "720p",
                    "prompt": {"head": prompt.strip()},
                    "skill_sections": version,
                    "trimmed": version > 1,
                },
                created_at=_now(),
            )
        )
    session.flush()
    login(client, world["owner"], settings)

    listed = client.get(f"/api/v1/clips/{clip_id}/revisions")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 2

    first, second = listed.json()["items"][1]["id"], listed.json()["items"][0]["id"]
    diff = client.get(f"/api/v1/revisions/{first}/diff/{second}")
    assert diff.status_code == 200
    body = diff.json()
    assert "急推" in body["prompt_diff"]
    assert body["cost_delta"] == "0.000000"
    assert body["params_diff"]["version"] == ["v1", "v2"]
    assert body["params_diff"]["prompt"] == [{"head": "镜头1（0-3秒）：远景"}, {"head": "镜头1（0-3秒）：远景急推"}]
    assert body["params_diff"]["trimmed"] == [False, True]


def test_health_reports_db(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True, "migrated": True}


def test_startup_exits_on_missing_config() -> None:
    from manju.config import Settings as S
    from manju.main import _startup_problems

    problems = _startup_problems(
        S(
            database_url="mysql+pymysql://root:root@127.0.0.1:3306/manju_test?charset=utf8mb4",
            jwt_secret="short",
            csrf_origins=(),
            media_root=__import__("pathlib").Path("Z:/definitely-missing"),
        )
    )
    assert any("JWT_SECRET" in item for item in problems)
    assert any("CSRF_ORIGINS" in item for item in problems)
    assert any("MEDIA_ROOT" in item for item in problems)


@pytest.mark.parametrize("path", ["/api/v1/projects", "/api/v1/auth/me"])
def test_no_cache_surprises(client: TestClient, path: str) -> None:
    """匿名访问只读端点一律 401，不泄露资源是否存在（`02` §2.1 第 3 条）。"""
    assert client.get(path).status_code == 401


# ---------------------------------------------------------------- 与文档对齐后补的用例


def test_cost_board_is_lead_only(client: TestClient, world: dict, settings: Settings) -> None:
    """`02` §2.2：成本看板 user ❌ / lead ✅ / admin ✅。"""
    url = f"/api/v1/projects/{world['project'].id}/cost"
    login(client, world["member"], settings)
    denied = client.get(url)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERM_DENIED"

    login(client, world["owner"], settings)
    assert client.get(url).status_code == 200

    login(client, world["admin"], settings)
    assert client.get(url).status_code == 200


def test_lock_requires_write_permission(client: TestClient, world: dict, settings: Settings) -> None:
    """`02` §3.8 把锁的权限标成 write：只读用户不该能占编辑提示。"""
    payload = {"target_kind": "clip", "target_id": world["clip"].id}
    login(client, world["viewer"], settings)
    denied = client.post("/api/v1/locks/acquire", json=payload)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERM_READONLY_PROJECT"

    login(client, world["member"], settings)
    assert client.post("/api/v1/locks/acquire", json=payload).status_code == 200


def test_capabilities_are_computed(client: TestClient, world: dict, settings: Settings) -> None:
    """`02` §5.2：前端只依赖 capabilities，所以它必须是真实值。"""
    login(client, world["viewer"], settings)
    assert client.get("/api/v1/auth/me").json()["capabilities"]["can_write_any_project"] is False

    login(client, world["member"], settings)
    assert client.get("/api/v1/auth/me").json()["capabilities"]["can_write_any_project"] is True

    login(client, world["owner"], settings)
    assert client.get("/api/v1/auth/me").json()["capabilities"]["can_create_project"] is True

    fresh = client.post("/api/v1/auth/register", json={"email": "fresh@example.com", "password": "devpass12345"})
    assert fresh.status_code == 201
    capabilities = client.get("/api/v1/auth/me").json()["capabilities"]
    assert capabilities["can_write_any_project"] is False
    assert capabilities["can_write_global"] is False


@pytest.mark.parametrize("duration", ["1.2e1", "１２", "12.0000001", "NaN", "12,5"])
def test_numeric_string_forms_rejected(
    client: TestClient, world: dict, settings: Settings, duration: str
) -> None:
    """`03` §6.2：科学计数法、全角、超存储精度一律拒，不静默截断。"""
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/episodes/{world['episode'].id}/clips", json={"duration_s": duration})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_RANGE"


def test_hidden_and_control_chars_rejected(client: TestClient, world: dict, settings: Settings) -> None:
    """`03` §6.1-2：零宽 / 双向控制符在标识性字段拒，正文里除换行制表符外也拒。"""
    login(client, world["owner"], settings)
    invisible = client.post("/api/v1/projects", json={"title": "a\u200bb"})
    assert invisible.status_code == 422
    assert invisible.json()["error"]["code"] == "VALIDATION_SHAPE"
    bidi = client.post("/api/v1/projects", json={"title": "\u202eabc"})
    assert bidi.status_code == 422

    control = client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        json={"prompt": "正常\x00异常"},
        headers={"If-Match": '"1"'},
    )
    assert control.status_code == 422
    newline_ok = client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        json={"prompt": "镜头1\n镜头2\t结束"},
        headers={"If-Match": '"1"'},
    )
    assert newline_ok.status_code == 200


def test_title_is_stripped(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    created = client.post("/api/v1/projects", json={"title": "  带空格的标题  "})
    assert created.status_code == 201
    assert created.json()["title"] == "带空格的标题"
    blank = client.post("/api/v1/projects", json={"title": "   "})
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "VALIDATION_SHAPE"


def test_duplicate_query_param_takes_last(client: TestClient, world: dict, settings: Settings) -> None:
    """`03` §6.7：同一 query 参数重复时取**最后**一个（框架默认，实测 Starlette 1.6）。"""
    login(client, world["owner"], settings)
    for title in ("第二个项目", "第三个项目"):
        assert client.post("/api/v1/projects", json={"title": title}).status_code == 201
    page = client.get("/api/v1/projects?limit=200&limit=1")
    assert page.status_code == 200
    assert len(page.json()["items"]) == 1


# ---------------------------------------------------------------- domain 不变量（01 §5.4）


def test_illegal_clip_status_transition(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    created = client.post(f"/api/v1/episodes/{world['episode'].id}/clips", json={"duration_s": "5.00"})
    clip_id = created.json()["id"]
    jumped = client.patch(
        f"/api/v1/clips/{clip_id}", json={"status": "approved"}, headers={"If-Match": '"1"'}
    )
    assert jumped.status_code == 409
    assert jumped.json()["error"]["code"] == "JOB_STATE_INVALID"


def test_prompted_requires_shots(client: TestClient, world: dict, settings: Settings) -> None:
    """draft 可以没分镜，迁到 prompted（分镜完成）就不行。"""
    login(client, world["owner"], settings)
    created = client.post(f"/api/v1/episodes/{world['episode'].id}/clips", json={"duration_s": "5.00"})
    clip_id = created.json()["id"]
    denied = client.patch(
        f"/api/v1/clips/{clip_id}", json={"status": "prompted"}, headers={"If-Match": '"1"'}
    )
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "VALIDATION_INVARIANT"
    assert "shots 不能为空" in " ".join(denied.json()["error"]["detail"]["problems"])


def test_shots_must_close_against_duration(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = f"/api/v1/clips/{world['clip'].id}"  # 该片段时长 12.00
    broken = client.patch(
        url,
        json={"shots": [{"no": 1, "start": "0", "end": "9.00", "desc": "只到 9 秒"}]},
        headers={"If-Match": '"1"'},
    )
    assert broken.status_code == 422
    assert "相差超过" in " ".join(broken.json()["error"]["detail"]["problems"])
    ok = client.patch(
        url,
        json={"shots": [{"no": 1, "start": "0", "end": "12.00", "desc": "整段一镜"}]},
        headers={"If-Match": '"1"'},
    )
    assert ok.status_code == 200


def test_dialogue_must_be_embedded_in_prompt(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = f"/api/v1/clips/{world['clip'].id}"
    dialogue = [{"speaker": "system", "text": "退钱", "shot_no": 1}]
    denied = client.patch(
        url,
        json={"prompt": "镜头1（0-12秒）：她在泥里吼", "dialogue": dialogue},
        headers={"If-Match": '"1"'},
    )
    assert denied.status_code == 422
    assert "没有内嵌" in " ".join(denied.json()["error"]["detail"]["problems"])
    ok = client.patch(
        url,
        json={"prompt": "镜头1（0-12秒）：她在泥里吼「退钱」", "dialogue": dialogue},
        headers={"If-Match": '"1"'},
    )
    assert ok.status_code == 200


def test_slot_asset_must_belong_to_project(
    client: TestClient, world: dict, settings: Settings, session: Session
) -> None:
    from manju.models import Asset, Project

    other = Project(
        title="别的项目",
        owner_id=world["owner"].id,
        status="active",
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(other)
    session.flush()
    alien = Asset(
        project_id=other.id,
        kind="image",
        path="other/x.png",
        sha256="a" * 64,
        bytes=10,
        source="seed",
        created_at=_now(),
    )
    session.add(alien)
    session.flush()

    login(client, world["owner"], settings)
    denied = client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        json={"ref_slots": [{"idx": 1, "kind": "image", "asset_id": alien.id, "role": "实景"}]},
        headers={"If-Match": '"1"'},
    )
    assert denied.status_code == 422
    assert "属于别的项目" in " ".join(denied.json()["error"]["detail"]["problems"])


def test_acceptance_id_must_be_unique(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    duplicated = [
        {"id": "a1", "criterion": "第一条"},
        {"id": "a1", "criterion": "第二条"},
    ]
    denied = client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        json={"acceptance": duplicated},
        headers={"If-Match": '"1"'},
    )
    assert denied.status_code == 422
    assert "id 重复" in " ".join(denied.json()["error"]["detail"]["problems"])
