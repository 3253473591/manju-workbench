"""引擎构造。只做一件事：把 DATABASE_URL 变成 Engine。

默认指向本地 `manju_dev`（`docker compose up` 起的 MySQL 8.4 或本机任一 MySQL）。
驱动固定 `pymysql`：同步、纯 Python、不需要编译。等 FastAPI 侧真正需要并发时再引入
`asyncmy`（当前 Python 3.13 还没有它的 wheel），届时只换 URL 前缀，模型不用动。
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine

DEFAULT_URL = "mysql+pymysql://root:root@127.0.0.1:3306/manju_dev?charset=utf8mb4"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None, echo: bool = False) -> Engine:
    # project 行锁负责入队串行；锁后查询必须能看到刚提交的任务，且空队列查询不应锁住索引间隙。
    return create_engine(url or database_url(), pool_pre_ping=True, echo=echo, isolation_level="READ COMMITTED",
                         connect_args={"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30})
