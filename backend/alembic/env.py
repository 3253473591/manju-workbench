"""Alembic 环境。

DATABASE_URL 与应用共用同一份配置（app.core.config），避免迁移和运行时连到不同的库。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import Text, engine_from_config, pool
from sqlmodel import SQLModel
from sqlmodel.sql.sqltypes import AutoString

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.db.engine import import_models  # noqa: E402

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

import_models()
target_metadata = SQLModel.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """把 SQLModel 的 AutoString 渲染成 sa.String。

    默认渲染会写出 `sqlmodel.sql.sqltypes.AutoString(...)`，但不会自动补
    `import sqlmodel`，迁移一跑就 NameError；而且 AutoString 本就是 String 的别名，
    直接落成 sa.String 更直白，也便于迁移文件脱离 SQLModel 独立阅读。
    """
    module = type(obj).__module__ or ""
    if type_ == "type" and module.startswith("sqlmodel"):
        length = getattr(obj, "length", None)
        if length is None:
            return "sa.Text()"
        return f"sa.String(length={length})"
    return False


def compare_type(
    context: object,
    inspected_column: object,
    metadata_column: object,
    inspected_type: object,
    metadata_type: object,
) -> bool | None:
    """AutoString 与 TEXT 视为同一种类型。

    无长度上限的 str 字段在 metadata 里是 SQLModel 的 AutoString，落库后 MySQL 读回来
    是 TEXT。两者语义相同，若不屏蔽，`alembic check` 会永远报一堆假漂移。
    """
    if isinstance(metadata_type, AutoString) and isinstance(inspected_type, Text):
        return False
    return None


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=compare_type,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=compare_type,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
