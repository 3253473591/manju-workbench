"""clip.title + asset.thumb_path

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18

两个可空列，都是为了把静态原型里的"标注待补"变成真实字段：
- `clip.title`：分镜表标题列（原表只有 `source_idx`，标题只能存在 seed 解析结果里）
- `asset.thumb_path`：镜头墙的缩略图（原表连 thumb 概念都没有）

`01` §7 只允许"加表 / 加可空列 / 加索引"，两条都符合。MySQL DDL 非事务性，
所以用 inspector 先查再建，允许半应用状态下重跑。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "title" not in {column["name"] for column in inspector.get_columns("clip")}:
        op.add_column("clip", sa.Column("title", sa.String(200), nullable=True))
    if "thumb_path" not in {column["name"] for column in inspector.get_columns("asset")}:
        op.add_column("asset", sa.Column("thumb_path", sa.String(512), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "thumb_path" in {column["name"] for column in inspector.get_columns("asset")}:
        op.drop_column("asset", "thumb_path")
    if "title" in {column["name"] for column in inspector.get_columns("clip")}:
        op.drop_column("clip", "title")
