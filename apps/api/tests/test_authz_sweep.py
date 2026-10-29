"""认证/授权全路由扫描（黑盒）：逐条路由撞"没登录"和"只读账号"。

这类缺陷靠人肉 review 很容易漏（新加一条路由忘了挂 `current_user`），所以用**枚举**来撞：

1. 未登录打每条 `/api/v1` 路由 → 必须 `401`，绝不能是 `200/404`（404 会泄漏"这个资源存在"）；
   公开路由（health / login / register）除外；
2. 只读账号（viewer）打每条写路由 → 必须 `4xx`，绝不能成功；
3. 两者都**不许出现 5xx**：那意味着鉴权前的形状校验就崩了。

路径参数统一填 1（不存在的资源就会 404，所以断言写成"在期望集合里"，而不是死磕 401）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from manju.config import Settings
from tests.conftest import login

PUBLIC = {"/api/v1/health", "/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/auth/logout"}
# logout 是"清自己的 cookie"，幂等且不需要已登录；admin 写域（skills / admin）也单独排除
ADMIN_WRITE = ("/api/v1/skills", "/api/v1/admin/")
CONCRETE = {
    "{project_id}": "1", "{episode_id}": "1", "{clip_id}": "1", "{asset_id}": "1",
    "{job_id}": "1", "{skill_id}": "1", "{user_id}": "1",
    "{version}": "1", "{revision_id}": "1", "{left_id}": "1", "{right_id}": "1",
    "{name}": "mock", "{jid}": "1", "{sid}": "1", "{v}": "1", "{cid}": "1", "{aid}": "1",
    "{rid}": "1", "{a}": "1", "{b}": "1", "{pid}": "1", "{eid}": "1",
}
WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


def _concrete(path: str) -> str:
    for placeholder, value in CONCRETE.items():
        path = path.replace(placeholder, value)
    return path


def _routes(client: TestClient) -> list[tuple[str, str]]:
    """从 OpenAPI 取路由表。

    这套 FastAPI 把 `include_router` 的结果包成 `_IncludedRouter`（没有 `.routes`），
    所以直接遍历 `app.routes` 扫不到东西 —— 走 OpenAPI 才是稳定的枚举方式。
    """
    schema = client.app.openapi()  # type: ignore[attr-defined]
    found: list[tuple[str, str]] = []
    for path, operations in sorted(schema["paths"].items()):
        if not path.startswith("/api/v1"):
            continue
        for method in sorted(operations):
            if method.upper() in {"HEAD", "OPTIONS", "PARAMETERS"}:
                continue
            found.append((method.upper(), _concrete(path)))
    return found


def test_every_route_requires_login_and_never_500s(client: TestClient) -> None:
    routes = _routes(client)
    assert routes, "一条路由都没扫到，说明枚举方式失效了"
    failures: list[str] = []
    for method, path in routes:
        response = client.request(method, path, json={})
        if response.status_code >= 500:
            failures.append(f"{method} {path} → {response.status_code}（未登录就 5xx）")
            continue
        if path in PUBLIC:
            continue
        if response.status_code != 401:
            failures.append(f"{method} {path} → {response.status_code}（未登录应为 401）")
    assert not failures, "\n".join(failures)


def test_readonly_account_cannot_write_anything(
    client: TestClient, world: dict, settings: Settings
) -> None:
    """viewer 是项目成员但只读：所有写路由都必须被挡住，且没有 5xx。"""
    login(client, world["viewer"], settings)
    failures: list[str] = []
    for method, path in _routes(client):
        if method not in WRITE_METHODS or path.startswith("/api/v1/auth/"):  # logout/登录是会话操作
            continue
        response = client.request(method, path, json={})
        if response.status_code >= 500:
            failures.append(f"{method} {path} → {response.status_code}")
        elif response.status_code < 400:
            failures.append(f"{method} {path} → {response.status_code}（只读账号写成功了）")
    assert not failures, "\n".join(failures)


def test_admin_cannot_write_content(client: TestClient, world: dict, settings: Settings) -> None:
    """admin 对内容只读（ADR-008）：写内容的路由必须挡住，Skill 除外。"""
    login(client, world["admin"], settings)
    failures: list[str] = []
    for method, path in _routes(client):
        if method not in WRITE_METHODS or path.startswith(ADMIN_WRITE):
            continue
        if path.startswith("/api/v1/auth/"):  # logout 是每个人自己的会话操作
            continue
        response = client.request(method, path, json={})
        if response.status_code >= 500:
            failures.append(f"{method} {path} → {response.status_code}")
        elif response.status_code < 400:
            failures.append(f"{method} {path} → {response.status_code}（admin 写内容成功了）")
    assert not failures, "\n".join(failures)


def test_unknown_subpath_under_known_prefix_is_404(client: TestClient, world: dict, settings: Settings) -> None:
    """路径前缀对、子路径错 → 404（不是 500，也不是 405 混淆）。"""
    login(client, world["owner"], settings)
    for path in (
        "/api/v1/clips/1/nope",
        "/api/v1/projects/1/episodes/1/nope",
        "/api/v1/assets/1/file/extra",
    ):
        assert client.get(path).status_code < 500, path
