"""数据库引擎与会话。

MySQL 8：连接串带 charset=utf8mb4，启用 pool_pre_ping 避免空闲后拿到死连接。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session, SQLModel

from app.core.config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = make_url(settings.DATABASE_URL)
        connect_args: dict[str, object] = {}
        if url.get_backend_name() == "sqlite":
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.DATABASE_URL,
            echo=settings.DB_ECHO,
            pool_pre_ping=url.get_backend_name() != "sqlite",
            connect_args=connect_args,
            future=True,
        )
    return _engine


def reset_engine() -> None:
    """测试用：丢弃已缓存的引擎，让新的 DATABASE_URL 生效。"""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def ensure_database() -> bool:
    """目标库不存在时创建它。返回是否本次真正创建。"""
    url = make_url(settings.DATABASE_URL)
    backend = url.get_backend_name()
    if backend == "sqlite":
        return False
    database = url.database
    if not database:
        return False

    # 注意：URL.set(database=None) 表示「不修改」，必须显式置空串才会去掉默认库。
    server_url = url.set(database="")
    admin = create_engine(server_url, pool_pre_ping=True, future=True)
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text(
                    "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
                    "WHERE SCHEMA_NAME = :name"
                ),
                {"name": database},
            ).scalar()
            if exists:
                return False
            conn.execute(
                text(
                    f"CREATE DATABASE `{database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
            conn.commit()
            return True
    finally:
        admin.dispose()


def import_models() -> None:
    """导入所有模型，让 SQLModel.metadata 认识全部表。"""
    import app.models  # noqa: F401


def create_all() -> None:
    import_models()
    SQLModel.metadata.create_all(get_engine())


def drop_all() -> None:
    import_models()
    SQLModel.metadata.drop_all(get_engine())


def get_session() -> Iterator[Session]:
    """FastAPI 依赖。"""
    with Session(get_engine()) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本与后台 worker 用的会话上下文，异常时回滚。"""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
