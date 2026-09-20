"""Content 领域：大纲 / 人物设定 / 剧本 / Clip 与其不可变历史版本。

版本语义（PRD §4）：草稿（document.body）与不可变历史版本（content_revisions）分开保存。
保存或生成前与选中版本比较，实际修改才新增版本；首次保存为 v1；切换历史版本不新增版本。
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Field

from app.core.utils import new_ulid
from app.models.common import SoftDeleteMixin, TimestampMixin, VersionMixin, enum_field
from app.models.enums import DocumentKind, DocumentStatus, RevisionSource


class ContentDocument(SoftDeleteMixin, VersionMixin, TimestampMixin, table=True):
    __tablename__ = "content_documents"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    episode_id: str = Field(foreign_key="episodes.id", index=True, max_length=26)
    kind: DocumentKind = enum_field(DocumentKind, DocumentKind.outline, length=16)
    parent_document_id: Optional[str] = Field(default=None, max_length=26, index=True)
    title: str = Field(default="", max_length=200)
    # 草稿区：用户正在编辑但可能还没保存成版本的正文
    body: str = Field(default="")
    status: DocumentStatus = enum_field(DocumentStatus, DocumentStatus.draft, length=16)
    # 指向 content_revisions.id；两侧互相引用，因此这里不加外键约束，避免建表成环。
    current_revision_id: Optional[str] = Field(default=None, max_length=26)
    # 业务关联（PRD §4「继承」）：绑定的大纲版本、设定快照、来源剧本版本
    bound_outline_revision_id: Optional[str] = Field(default=None, max_length=26)
    bound_setting_revision_id: Optional[str] = Field(default=None, max_length=26)
    source_revision_id: Optional[str] = Field(default=None, max_length=26)
    node_id: Optional[str] = Field(default=None, max_length=26, index=True)


class ContentRevision(TimestampMixin, table=True):
    """不可变历史版本。写入后不再修改。"""

    __tablename__ = "content_revisions"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    document_id: str = Field(
        foreign_key="content_documents.id", index=True, max_length=26
    )
    revision_no: int = Field(default=1, nullable=False)
    source: RevisionSource = enum_field(RevisionSource, RevisionSource.user, length=16)
    title: str = Field(default="", max_length=200)
    body: str = Field(default="")
    metadata_json: str = Field(default="{}")
    created_by: Optional[str] = Field(default=None, max_length=26)


class CharacterProfile(SoftDeleteMixin, VersionMixin, TimestampMixin, table=True):
    """人物设定。字段与 PRD §5.1 一致；不表示每个字段都要出图。"""

    __tablename__ = "character_profiles"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    project_id: str = Field(foreign_key="projects.id", index=True, max_length=26)
    episode_id: str = Field(foreign_key="episodes.id", index=True, max_length=26)
    document_id: Optional[str] = Field(default=None, max_length=26, index=True)
    name: str = Field(default="", max_length=200)
    identity: str = Field(default="", max_length=500)
    personality: str = Field(default="", max_length=1000)
    appearance: str = Field(default="", max_length=1000)
    hair: str = Field(default="", max_length=500)
    base_outfit: str = Field(default="", max_length=1000)
    outfit_variants: str = Field(default="", max_length=2000)
    signature_props: str = Field(default="", max_length=1000)
    source_revision_id: Optional[str] = Field(default=None, max_length=26)


class Clip(SoftDeleteMixin, VersionMixin, TimestampMixin, table=True):
    """Clip 是一级节点。内部镜头存在 clip_shots，不另建节点。"""

    __tablename__ = "clips"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    episode_id: str = Field(foreign_key="episodes.id", index=True, max_length=26)
    document_id: Optional[str] = Field(default=None, max_length=26, index=True)
    source_revision_id: Optional[str] = Field(default=None, max_length=26)
    clip_no: int = Field(default=1, nullable=False)
    title: str = Field(default="", max_length=200)
    duration_ms: int = Field(default=0)
    characters_json: str = Field(default="[]")
    location_name: str = Field(default="", max_length=200)
    status: str = Field(default="draft", max_length=32)
    body: str = Field(default="")


class ClipShot(TimestampMixin, table=True):
    __tablename__ = "clip_shots"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    clip_id: str = Field(foreign_key="clips.id", index=True, max_length=26)
    shot_no: int = Field(default=1, nullable=False)
    start_ms: int = Field(default=0)
    end_ms: int = Field(default=0)
    visual_text: str = Field(default="")
    camera_text: str = Field(default="")
    dialogue_text: str = Field(default="")
    sound_text: str = Field(default="")
