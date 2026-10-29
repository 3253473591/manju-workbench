"""event_log —— SSE 的跨副本事件日志

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20

进程内总线保留（低延迟），这张表只补"别的副本发的事件"。
"""

from __future__ import annotations

from alembic import op

from manju.models import EventLog

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    EventLog.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    EventLog.__table__.drop(bind=op.get_bind(), checkfirst=True)
