"""测试脚手架：真 MySQL + alembic 建表 + 每用例事务回滚。

不用 SQLite：模型的 `BIGINT UNSIGNED`、生成列、`CHECK` 都是 MySQL 语义，
换成 SQLite 测出来的"通过"不代表线上通过。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

API_ROOT = Path(__file__).resolve().parents[1]
TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "mysql+pymysql://root:root@127.0.0.1:3306/manju_test?charset=utf8mb4",
)

from manju.config import COOKIE_NAME, Settings  # noqa: E402  (需要先算好 TEST_URL)
from manju.deps import get_session, get_settings  # noqa: E402
from manju.db import make_engine  # noqa: E402
from manju.main import create_app  # noqa: E402
from manju.models import Clip, Episode, Project, ProjectMember, User  # noqa: E402
from manju.security import hash_password, issue_token  # noqa: E402


def _ensure_database() -> None:
    url = make_url(TEST_URL)
    server = create_engine(url.set(database=""))
    with server.begin() as connection:
        # 每次从干净库开始：上一次运行被 Ctrl-C/超时打断时，端点里的 commit 会留下已提交的行，
        # 下一次再跑就会出现"idx 已被占用"这类与用例无关的失败（实测：kill 后重跑撞 uk_clip_ep_idx）。
        connection.execute(text(f"DROP DATABASE IF EXISTS `{url.database}`"))
        connection.execute(
            text(
                f"CREATE DATABASE `{url.database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        )
    server.dispose()


@pytest.fixture(scope="session")
def schema() -> Iterator[str]:
    _ensure_database()
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_URL
    config = Config(str(API_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    try:
        yield TEST_URL
    finally:
        command.downgrade(config, "base")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture(scope="session")
def engine(schema: str) -> Iterator[Engine]:
    engine = make_engine(schema)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="session")
def media_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """每个测试会话一个临时 MEDIA_ROOT：取图测试要往里面写文件，不能写进仓库。"""
    return tmp_path_factory.mktemp("media")


@pytest.fixture
def settings(media_root: Path) -> Settings:
    return Settings(
        database_url=TEST_URL,
        jwt_secret="test-secret-test-secret-test-secret",
        csrf_origins=("http://localhost:5173",),
        media_root=media_root,
        worker=False,
    )


@pytest.fixture(autouse=True)
def _isolate_event_store() -> Iterator[None]:
    """每个用例都把全局总线退回"纯进程内"。

    `test_audit_regressions.py` 里有一处 `with TestClient(app)` 会真跑 lifespan，而 lifespan 会把
    `BUS` 绑上真的事件日志（`event_log`，多副本用）。不摘掉的话，之后每个用例的 `publish`
    都会去 INSERT —— 测试的项目行没提交，FK 检查要等锁，全量会越来越慢直到卡死（实测）。
    """
    from manju.events import BUS

    BUS.bind_store(None)
    yield
    BUS.bind_store(None)


@pytest.fixture
def client(session: Session, settings: Settings) -> Iterator[TestClient]:
    """不进 lifespan：每个用例跑一遍启动校验会各建两个 Engine，150+ 用例下会把本机
    临时端口/缓冲打满（Windows 上报 WinError 10055，且每次落在不同用例上）。
    启动校验本身由 `test_startup_exits_on_missing_config` 直接调 `_startup_problems` 覆盖。

    `app.state.sessionmaker` 由 lifespan 建，测试里跳过 lifespan，所以这里补一个**不关会话**的
    工厂：SSE 端点在流开始前用它做一次性鉴权（它不能挂 `get_session` 依赖，见 `api/events.py`）。
    """
    app = create_app(settings)
    app.state.settings = settings
    app.state.sessionmaker = lambda: contextlib.nullcontext(session)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def make_user(session: Session, email: str, role: str = "user", password: str = "devpass12345") -> User:
    user = User(
        email=email,
        pwd_hash=hash_password(password),
        role=role,
        is_active=1,
        created_at=_now(),
    )
    session.add(user)
    session.flush()
    return user


def login(client: TestClient, user: User, settings: Settings) -> None:
    client.cookies.set(COOKIE_NAME, issue_token(user.id, settings))


@pytest.fixture
def world(session: Session) -> dict:
    """一套最小世界：owner(lead) + member + viewer + admin + 一个项目/集/片段。"""
    owner = make_user(session, "owner@example.com", "lead")
    member = make_user(session, "member@example.com", "user")
    viewer = make_user(session, "viewer@example.com", "user")
    admin = make_user(session, "admin@example.com", "admin")

    project = Project(
        title="师尊别砍我系统",
        owner_id=owner.id,
        genre="修仙",
        tone="荒诞反差喜剧",
        logline="师徒 vs 系统",
        status="active",
        version=3,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(project)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=member.id, role="member", created_at=_now()))
    session.add(ProjectMember(project_id=project.id, user_id=viewer.id, role="viewer", created_at=_now()))

    episode = Episode(
        project_id=project.id,
        idx=1,
        title="第一集",
        synopsis="猪窝接生",
        status="storyboarded",
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(episode)
    session.flush()

    clip = Clip(
        episode_id=episode.id,
        idx=1,
        source_idx=1,
        duration_s="12.00",
        status="approved",
        prompt="将参考图一中的荒郊猪窝…",
        shots_json=[{"no": 1, "start": 0, "end": 12, "desc": "远景急推"}],
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return {
        "owner": owner,
        "member": member,
        "viewer": viewer,
        "admin": admin,
        "project": project,
        "episode": episode,
        "clip": clip,
    }
