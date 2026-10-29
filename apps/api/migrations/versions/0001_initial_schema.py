"""initial schema: 13 tables

Revision ID: 0001
Revises:
Create Date: 2026-10-18

初版 schema 由 `app.models.Base.metadata` 直接建，不手抄 13 段 CREATE TABLE ——
手抄的副本一定会在第二次改字段时和模型漂移，而 `01` §4 才是权威文本，模型是它的可执行镜像。
`checkfirst=True` 让这条迁移在"表已建、alembic_version 没写"的半应用状态下也能重跑
（MySQL DDL 非事务性，这是 `01` §7.3 的硬要求）。

后续迁移请 `alembic revision -m ...` 生成显式 DDL，不要再沿用本文件的整库同步。
"""

from __future__ import annotations

from alembic import op

from manju.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
