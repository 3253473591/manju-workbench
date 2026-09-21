"""Add durable generation worker leases and recovery audit fields."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_generation_leases"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("generation_submissions", sa.Column("claimed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "generation_submissions",
        sa.Column("claimed_by", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column("generation_submissions", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    op.add_column(
        "generation_submissions",
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
    )
    # MySQL/InnoDB 不允许 TEXT/BLOB/JSON 列带 DEFAULT（errno 1101），
    # 所以先建为可空、回填历史行，再收紧成 NOT NULL。
    op.add_column(
        "generation_submissions", sa.Column("recovery_reason", sa.Text(), nullable=True)
    )
    op.execute("UPDATE generation_submissions SET recovery_reason = '' WHERE recovery_reason IS NULL")
    op.alter_column(
        "generation_submissions",
        "recovery_reason",
        existing_type=sa.Text(),
        nullable=False,
    )
    # 稳定的节点级生成锁键。历史行回填成 node_id，保持既有语义不变。
    op.add_column(
        "generation_submissions", sa.Column("lock_key", sa.String(length=26), nullable=True)
    )
    op.execute("UPDATE generation_submissions SET lock_key = node_id WHERE node_id IS NOT NULL")
    op.create_index("ix_generation_submissions_claimed_at", "generation_submissions", ["claimed_at"])
    op.create_index("ix_generation_submissions_heartbeat_at", "generation_submissions", ["heartbeat_at"])
    op.create_index("ix_generation_submissions_lock_key", "generation_submissions", ["lock_key"])
    # 按状态过滤是生成链路上最高频的查询（租约回收、原子领取、节点锁）。
    # 没有索引会扫全表，在并发 worker 下放大间隙锁范围、更容易死锁。
    op.create_index(
        "ix_generation_submissions_logical_status",
        "generation_submissions",
        ["logical_status"],
    )

    # 供应商任务恢复所需的审计字段
    op.add_column(
        "generation_attempts",
        sa.Column("reconcile_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "generation_attempts", sa.Column("last_reconciled_at", sa.DateTime(), nullable=True)
    )
    op.create_index(
        "ix_generation_attempts_last_reconciled_at", "generation_attempts", ["last_reconciled_at"]
    )
    # 注意：`provider_task_id` 上已经有 0001 建的唯一约束
    # `uq_attempt_provider_task`，它自带索引。这里不要再建普通索引：
    # 模型里没有声明它，`alembic check` 会报「New upgrade operations detected: remove_index」。


def downgrade() -> None:
    op.drop_index("ix_generation_attempts_last_reconciled_at", table_name="generation_attempts")
    for name in ("last_reconciled_at", "reconcile_count"):
        op.drop_column("generation_attempts", name)

    op.drop_index("ix_generation_submissions_logical_status", table_name="generation_submissions")
    op.drop_index("ix_generation_submissions_lock_key", table_name="generation_submissions")
    op.drop_index("ix_generation_submissions_heartbeat_at", table_name="generation_submissions")
    op.drop_index("ix_generation_submissions_claimed_at", table_name="generation_submissions")
    for name in ("lock_key", "recovery_reason", "recovery_count", "heartbeat_at", "claimed_by", "claimed_at"):
        op.drop_column("generation_submissions", name)
