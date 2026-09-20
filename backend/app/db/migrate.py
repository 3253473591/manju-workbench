"""用 Alembic 以编程方式建库升级。

启动时自动跑迁移，保证 `run.cmd -m uvicorn app.main:app` 开箱即用。
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from app.core.config import BACKEND_DIR


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("prepend_sys_path", str(BACKEND_DIR))
    return config


def upgrade_to_head() -> None:
    command.upgrade(alembic_config(), "head")


def downgrade_to_base() -> None:
    command.downgrade(alembic_config(), "base")


def current_revision() -> str | None:
    from alembic.runtime.migration import MigrationContext

    from app.db.engine import get_engine

    with get_engine().connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def has_alembic_version_table() -> bool:
    from sqlalchemy import inspect

    from app.db.engine import get_engine

    return "alembic_version" in inspect(get_engine()).get_table_names()


def is_up_to_date() -> bool:
    return has_alembic_version_table() and current_revision() is not None


def reset_to_head() -> None:
    """把库彻底重置到最新迁移。

    注意：`SQLModel.metadata.drop_all()` 不会删 `alembic_version`，如果只 drop_all 再
    upgrade，Alembic 会以为已经是最新版而什么都不做，表就建不出来。所以必须一并清掉版本表。
    """
    from sqlalchemy import text

    from app.db.engine import drop_all, get_engine

    drop_all()
    with get_engine().begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    upgrade_to_head()


__all__ = [
    "alembic_config",
    "current_revision",
    "downgrade_to_base",
    "has_alembic_version_table",
    "is_up_to_date",
    "reset_to_head",
    "upgrade_to_head",
]
