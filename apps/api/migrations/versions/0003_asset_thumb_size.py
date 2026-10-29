"""asset.thumb_w / thumb_h

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18

镜头墙要用缩略图的宽高做占位比例（`docs/前端视觉交付与对接清单.md` §2.1 #2）。
`w` / `h` 是**原片**尺寸，海报按长边 720 等比缩放后是另一组数，所以单独两列 —— 不要用
`w/h` 冒充，前端据此算 aspect-ratio 会偏差。

两条都是可空列，符合 `01` §7 的"只允许加表 / 加可空列 / 加索引"。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("asset")}
    for name in ("thumb_w", "thumb_h"):
        if name not in columns:
            op.add_column("asset", sa.Column(name, sa.Integer().with_variant(sa.Integer, "mysql"), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("asset")}
    for name in ("thumb_h", "thumb_w"):
        if name in columns:
            op.drop_column("asset", name)
