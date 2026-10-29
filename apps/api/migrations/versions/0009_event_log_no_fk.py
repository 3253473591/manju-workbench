"""event_log 去掉 project 外键

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20

原因见模型注释：`publish` 在 `submit`/`confirm` 的事务里执行，此时 `project` 行已被 FOR UPDATE 锁住，
FK 校验要父行 S 锁 → 等自己 → 30 s 超时，异常被吞后表现为"接口变慢、事件没落库"。
日志是投递通道，不是账本：孤儿行靠 `EVENT_KEEP_DAYS` 清理。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 新库（0008 时模型已去掉 FK）没有这个约束 —— 必须容忍，否则全新环境迁移直接失败
    bind = op.get_bind()
    existing = {fk["name"] for fk in inspect(bind).get_foreign_keys("event_log")}
    if "fk_event_project" in existing:
        op.drop_constraint("fk_event_project", "event_log", type_="foreignkey")


def downgrade() -> None:
    op.create_foreign_key(
        "fk_event_project", "event_log", "project", ["project_id"], ["id"], ondelete="CASCADE"
    )
