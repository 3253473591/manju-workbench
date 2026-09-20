"""Asset 领域：项目内共享的资产身份与 Clip 引用槽位。

资产身份在项目内共享；同名候选各有独立节点和物理文件，不互相覆盖（PRD §4）。
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Field, UniqueConstraint

from app.core.utils import new_ulid
from app.models.common import SoftDeleteMixin, TimestampMixin, enum_field
from app.models.enums import AssetStatus, AssetType


class Asset(SoftDeleteMixin, TimestampMixin, table=True):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "asset_type",
            "canonical_name",
            "variant_name",
            name="uq_asset_identity",
        ),
    )

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    project_id: str = Field(foreign_key="projects.id", index=True, max_length=26)
    asset_type: AssetType = enum_field(AssetType, AssetType.character, length=16)
    #: 规范名，如「林晚」「校门」「雨伞」
    canonical_name: str = Field(max_length=200, nullable=False)
    #: 变体名，如「冬装」；无变体时为空串，参与唯一键
    variant_name: str = Field(default="", max_length=200)
    #: 图种：立绘 / 三视图 / 面部特写 / 场景 / 道具；空表示尚未指定
    image_kind: str = Field(default="", max_length=32)
    status: AssetStatus = enum_field(AssetStatus, AssetStatus.planned, length=16)
    #: 该身份对应的图片方案节点（media_nodes.id），确认清单时创建
    media_node_id: Optional[str] = Field(default=None, max_length=26)


class AssetReference(TimestampMixin, table=True):
    """Clip 的参考资产槽位。移除引用不删除资产。"""

    __tablename__ = "asset_references"
    __table_args__ = (
        UniqueConstraint("clip_id", "asset_id", name="uq_asset_reference"),
    )

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    clip_id: str = Field(foreign_key="clips.id", index=True, max_length=26)
    asset_id: str = Field(foreign_key="assets.id", index=True, max_length=26)
    slot_no: int = Field(default=0)
    required: bool = Field(default=True)
    #: 引用来源：auto（规范名回填）/ manual（用户手动添加）
    source: str = Field(default="auto", max_length=32)
