"""媒体领域：物理文件、媒体节点（方案/结果）、生成时冻结的输入快照。"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Field

from app.core.utils import new_ulid
from app.models.common import SoftDeleteMixin, TimestampMixin, enum_field
from app.models.enums import DecodeStatus, MediaKind, MediaType


class MediaItem(SoftDeleteMixin, TimestampMixin, table=True):
    """每个物理文件一行。`file_key` 是相对 MEDIA_ROOT 的存储键，不重复使用业务名称。"""

    __tablename__ = "media_items"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    media_type: MediaType = enum_field(MediaType, MediaType.image, length=16)
    file_key: str = Field(max_length=500, unique=True, index=True)
    original_name: str = Field(default="", max_length=500)
    mime_type: str = Field(default="", max_length=120)
    size_bytes: int = Field(default=0)
    sha256: str = Field(default="", max_length=64, index=True)
    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)
    duration_ms: Optional[int] = Field(default=None)
    decode_status: DecodeStatus = enum_field(DecodeStatus, DecodeStatus.pending, length=16)
    #: 资产命名规范解析出来的图种与规范名，用于同名匹配
    parsed_kind: str = Field(default="", max_length=32)
    parsed_name: str = Field(default="", max_length=200)
    parsed_variant: str = Field(default="", max_length=200)
    created_by: Optional[str] = Field(default=None, max_length=26)


class MediaNode(SoftDeleteMixin, TimestampMixin, table=True):
    """方案节点保存提示词；结果节点每个只承载一个产物。"""

    __tablename__ = "media_nodes"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    episode_id: str = Field(foreign_key="episodes.id", index=True, max_length=26)
    media_kind: MediaKind = enum_field(MediaKind, MediaKind.image_plan, length=20)
    #: 结果节点才有产物
    media_item_id: Optional[str] = Field(default=None, max_length=26, index=True)
    #: 图生图的源图（永远是源图，产物不覆盖它）
    source_media_id: Optional[str] = Field(default=None, max_length=26)
    #: 当前选中的提示词版本
    prompt_version_id: Optional[str] = Field(default=None, max_length=26)
    #: 视频方案冻结的 Clip
    clip_id: Optional[str] = Field(default=None, max_length=26, index=True)
    #: 图片方案对应的资产身份
    asset_id: Optional[str] = Field(default=None, max_length=26, index=True)
    #: 图生图的目标名（Agent 建议，用户抽卡前确认）
    target_name: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    #: 屏蔽只影响当前节点
    blocked: bool = Field(default=False, nullable=False)
    #: 抽卡参数：seed、size、产品档位等
    seed: int = Field(default=-1)
    params_json: str = Field(default="{}")
    content_fingerprint: str = Field(default="", max_length=128)


class MediaSnapshot(TimestampMixin, table=True):
    """生成时冻结的媒体输入、参考槽位顺序与文件 hash。"""

    __tablename__ = "media_snapshots"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    media_node_id: str = Field(
        foreign_key="media_nodes.id", index=True, max_length=26
    )
    attempt_id: Optional[str] = Field(default=None, max_length=26, index=True)
    snapshot_json: str = Field(default="{}")
