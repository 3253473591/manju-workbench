"""pytest 基础设施。

必须在导入 app 之前设置环境变量，因为 settings 是模块级缓存的。

**测试库是强制指定的，不是「默认值」。**

早期版本这里用 `os.environ.setdefault("DATABASE_URL", ...)`。那意味着：只要你的 shell
里导出过 `DATABASE_URL`（比如你刚在同一个窗口里跑过后端 dev server），整轮测试就会
悄悄打到**开发/演示库**上——`reset_to_head()` 会把演示数据 drop 掉，而且正在运行的
dev server 与测试会同时改同一批表，表现就是这些看似无关的报错：

- `Table 'xxx.clips' doesn't exist`（另一进程正在 drop_all 的窗口里 TRUNCATE）
- `Table definition has changed, please retry transaction`（MySQL 1412：DDL 与查询并发）
- `Deadlock found when trying to get lock`
- 权限 / 项目 / 结果节点 / reconciler / 幂等等用例随机失败（读到了被截断或被重建的数据）

所以这里做了三件事：

1. **强制**写入 `DATABASE_URL`（不再 setdefault），并允许用 `MANJU_TEST_DATABASE_URL`
   显式换库；
2. 库名必须以 `_test` 结尾，否则拒绝运行——防止误伤演示库；
3. 整个会话持有一把 MySQL 命名锁（`GET_LOCK`），让并发跑同一套测试的两个进程串行化，
   而不是互相 TRUNCATE 与迁移。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

DEFAULT_TEST_DATABASE_URL = (
    "mysql+pymysql://root:root@127.0.0.1:3306/manju_director_test?charset=utf8mb4"
)
#: 换一个测试库请设这个，不要靠 DATABASE_URL（那份是给应用跑的）。
TEST_DATABASE_URL = (
    os.environ.get("MANJU_TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# `DEBUG` is a generic environment variable used by some shells/CI tools and
# may contain values such as "release". It must not override the boolean
# setting while pytest is importing the application configuration.
os.environ.pop("DEBUG", None)
os.environ.setdefault("APP_MODE", "mock")
os.environ.setdefault("WORKER_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.engine import (  # noqa: E402
    ensure_database,
    get_engine,
    import_models,
    session_scope,
)
from app.db.migrate import reset_to_head  # noqa: E402
from app.db.seed import ensure_model_configs  # noqa: E402
from app.services import auth_service  # noqa: E402

#: 这些表在测试间保留内容（模型配置是种子数据）
KEEP_TABLES = {"model_configs", "alembic_version"}

#: 并发保护：同一测试库同时只允许一个 pytest 会话做「清库 + 迁移 + 跑用例」。
_SESSION_LOCK_NAME = "manju_director_pytest_session"
_SESSION_LOCK_TIMEOUT_SECONDS = 1800
#: 会话级缓存的业务表列表（清表用），避免每个用例都重新 inspect。
_TRUNCATE_TABLES: list[str] = []


def _database_name(url: str) -> str:
    return make_url(url).database or ""


def _assert_is_test_database() -> None:
    name = _database_name(settings.DATABASE_URL)
    if not name.endswith("_test"):
        pytest.exit(
            f"拒绝运行：测试库名必须是 *_test，当前是 {name!r}（DATABASE_URL={settings.DATABASE_URL}）。\n"
            "这套用例会 drop / TRUNCATE 目标库，打到演示库上会直接毁掉演示数据。\n"
            "要换库请设置 MANJU_TEST_DATABASE_URL（例如 ...:3306/manju_director_test2）。",
            returncode=2,
        )


def _acquire_session_lock(connection) -> None:  # type: ignore[no-untyped-def]
    acquired = connection.execute(
        text("SELECT GET_LOCK(:name, :timeout)"),
        {"name": _SESSION_LOCK_NAME, "timeout": _SESSION_LOCK_TIMEOUT_SECONDS},
    ).scalar()
    if acquired != 1:
        pytest.exit(
            "另一个 pytest 会话正在使用同一个测试库（已等待 "
            f"{_SESSION_LOCK_TIMEOUT_SECONDS}s）。\n"
            "并发跑同一套测试会互相清表与迁移，表现为 "
            "`Table definition has changed` / `table doesn't exist` / `Deadlock found`。\n"
            "等它跑完再试，或把 MANJU_TEST_DATABASE_URL 指到另一个 *_test 库。",
            returncode=2,
        )


def _assert_schema_complete(engine) -> None:  # type: ignore[no-untyped-def]
    """迁移后必须与模型一致；缺表就明确报出来，而不是等用例随机 500。"""
    import_models()
    from sqlmodel import SQLModel

    from sqlalchemy import inspect

    expected = set(SQLModel.metadata.tables)
    actual = set(inspect(engine).get_table_names())
    missing = sorted(expected - actual)
    if missing:
        pytest.exit(
            f"测试库迁移不完整，缺少表：{missing}。\n"
            "请检查 backend/alembic/versions/ 下的迁移是否能从空库一路升到 head。",
            returncode=2,
        )


@pytest.fixture(scope="session", autouse=True)
def prepared_database() -> Iterator[None]:
    _assert_is_test_database()
    ensure_database()
    engine = get_engine()
    backend = make_url(settings.DATABASE_URL).get_backend_name()
    lock_connection = engine.connect()
    try:
        if backend == "mysql":
            _acquire_session_lock(lock_connection)
        reset_to_head()
        _assert_schema_complete(engine)
        with session_scope() as db:
            auth_service.ensure_bootstrap_admin(db)
            ensure_model_configs(db)

        from sqlalchemy import inspect

        global _TRUNCATE_TABLES
        _TRUNCATE_TABLES = [
            t for t in inspect(engine).get_table_names() if t not in KEEP_TABLES
        ]
        yield
    finally:
        try:
            if backend == "mysql":
                lock_connection.execute(
                    text("SELECT RELEASE_LOCK(:name)"), {"name": _SESSION_LOCK_NAME}
                )
        finally:
            lock_connection.close()


@pytest.fixture(autouse=True)
def clean_tables(prepared_database: None) -> None:
    """每个用例前清空业务表，避免用例之间互相污染计数。"""
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in _TRUNCATE_TABLES:
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
