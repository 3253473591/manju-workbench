"""job.request_hash —— 幂等键"同 key 异 body → 409" 的判据

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18

`03` §8.2 要求同 `Idempotency-Key` 但请求体不同时返回 `409 IDEM_KEY_REUSED`。
没有这一列就只能比对"有没有这个 key"，分不清"重放"和"拿旧 key 发新请求"。
`NOT NULL` 会卡住历史行，所以先加可空列（`01` §7：只允许加可空列）；存量 job 为空时跳过比对。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "request_hash" not in {column["name"] for column in inspector.get_columns("job")}:
        op.add_column("job", sa.Column("request_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "request_hash" in {column["name"] for column in inspector.get_columns("job")}:
        op.drop_column("job", "request_hash")
