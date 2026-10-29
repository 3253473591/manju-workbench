"""runtime_flag —— provider 通道的运行时开关（/admin/providers 的写操作）

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20

同 0006：直接建模型那张表，不手抄 CREATE TABLE（`01` §7.3）。
"""

from __future__ import annotations

from alembic import op

from manju.models import RuntimeFlag

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    RuntimeFlag.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    RuntimeFlag.__table__.drop(bind=op.get_bind(), checkfirst=True)
