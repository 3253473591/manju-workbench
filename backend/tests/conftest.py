"""pytest 基础设施。

必须在导入 app 之前设置环境变量，因为 settings 是模块级缓存的。
"""

from __future__ import annotations

import os
from pathlib import Path

TEST_DATABASE_URL = os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://root:root@127.0.0.1:3306/manju_director_test?charset=utf8mb4",
)
os.environ.setdefault("APP_MODE", "mock")
os.environ.setdefault("WORKER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.engine import ensure_database, get_engine, session_scope  # noqa: E402
from app.db.migrate import reset_to_head  # noqa: E402
from app.db.seed import ensure_model_configs  # noqa: E402
from app.services import auth_service  # noqa: E402

#: 这些表在测试间保留内容（模型配置是种子数据）
KEEP_TABLES = {"model_configs", "alembic_version"}


@pytest.fixture(scope="session", autouse=True)
def prepared_database() -> None:
    ensure_database()
    reset_to_head()
    with session_scope() as db:
        auth_service.ensure_bootstrap_admin(db)
        ensure_model_configs(db)


@pytest.fixture(autouse=True)
def clean_tables(prepared_database: None) -> None:
    """每个用例前清空业务表，避免用例之间互相污染计数。"""
    from sqlalchemy import inspect

    engine = get_engine()
    tables = [t for t in inspect(engine).get_table_names() if t not in KEEP_TABLES]
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in tables:
            connection.execute(text(f"TRUNCATE TABLE `{table}`"))
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    # 清空后重新 seed 管理员（bootstrap admin 属于测试需要的最小环境）
    with session_scope() as db:
        auth_service.ensure_bootstrap_admin(db)


@pytest.fixture(scope="session")
def client(prepared_database: None) -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


# --- 便捷工具 -----------------------------------------------------------


def register_user(
    client: TestClient,
    username: str,
    password: str = "secret123",
    display_name: str | None = None,
):
    return client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "password": password,
            "displayName": display_name or username,
        },
    )


def login(client: TestClient, username: str, password: str = "secret123") -> str:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["sessionToken"]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user_token(client: TestClient) -> str:
    register_user(client, "alice")
    return login(client, "alice")


@pytest.fixture
def manager_token(client: TestClient) -> str:
    register_user(client, "bob_manager")
    with session_scope() as db:
        from sqlmodel import select

        from app.models.enums import Role
        from app.models.identity import User

        user = db.exec(select(User).where(User.username == "bob_manager")).one()
        user.role = Role.manager
        db.add(user)
    return login(client, "bob_manager")


@pytest.fixture
def admin_token(client: TestClient) -> str:
    return login(client, settings.BOOTSTRAP_ADMIN_USERNAME, settings.BOOTSTRAP_ADMIN_PASSWORD)


@pytest.fixture
def data_dir() -> Path:
    path = settings.media_root
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def runner():
    """同步推进后台生成任务。

    生产环境靠 worker 线程轮询；测试里显式推进，避免 sleep 和不稳定等待。
    """

    def _run(max_rounds: int = 30) -> int:
        from app.db.engine import session_scope
        from app.services.generation_service import run_pending

        total = 0
        for _ in range(max_rounds):
            with session_scope() as db:
                processed = run_pending(db, limit=4)
            total += processed
            if processed == 0:
                break
        return total

    return _run


def accept(client: TestClient, response) -> str:  # type: ignore[no-untyped-def]
    """断言 202 并返回 submissionId。"""
    assert response.status_code == 202, response.text
    return response.json()["submissionId"]


def submission_of(client: TestClient, token: str, submission_id: str) -> dict:
    response = client.get(
        f"/api/v1/submissions/{submission_id}", headers=auth_header(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


def wait_succeeded(client: TestClient, token: str, submission_id: str, runner) -> dict:  # type: ignore[no-untyped-def]
    runner()
    detail = submission_of(client, token, submission_id)
    assert detail["submission"]["logicalStatus"] == "succeeded", detail["submission"]
    return detail
