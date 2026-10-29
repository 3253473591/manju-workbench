"""job_trace —— job 执行时间线（P1-4）

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19

建表走 `JobTrace.__table__.create(checkfirst=True)` 而不是手抄 CREATE TABLE：
手抄的副本在第二次改字段时一定和模型漂移，而这里只建**这一张**表，
不是 0001 那种整库同步（`01` §7.3 的 `checkfirst` 要求同样适用）。
"""

from __future__ import annotations

from alembic import op

from manju.models import JobTrace

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    JobTrace.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    JobTrace.__table__.drop(bind=op.get_bind(), checkfirst=True)
