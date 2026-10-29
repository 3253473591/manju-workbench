"""Alembic 环境。连接串来自 manju.db，不在 alembic.ini 里重复维护。

MySQL 的 DDL 不是事务性的，所以这里不依赖 `begin_transaction` 做回滚保证 ——
每条迁移必须自己能重复执行（`01` §7.3）。
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from manju.db import database_url
from manju.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
