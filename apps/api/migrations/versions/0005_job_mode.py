"""job.mode —— 记住这次提交是"默认模式"还是"全能模式"

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18

自动重试（`03` §4.1 的全能模式）只能对 `?mode=auto` 的 job 生效：默认模式要停在付费点
等人确认。状态机会把 `waiting_confirm` 抹掉，所以模式必须落库，不能从状态反推。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("job")}
    if "mode" not in columns:
        # 存量行按 default 记（它们确实都没走自动重试）
        op.add_column("job", sa.Column("mode", sa.String(16), nullable=False, server_default="default"))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "mode" in {column["name"] for column in inspector.get_columns("job")}:
        op.drop_column("job", "mode")
