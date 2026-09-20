"""Canvas 领域：节点、布局、依赖边。

布局与内容严格分离（domain-boundaries §4）：拖动/改尺寸只写 canvas_node_layout，
不产生内容版本，也不影响内容指纹。
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Field, UniqueConstraint

from app.core.utils import new_ulid
from app.models.common import SoftDeleteMixin, TimestampMixin, enum_field
from app.models.enums import CanvasNodeType, EdgeType


class CanvasNode(SoftDeleteMixin, TimestampMixin, table=True):
    """画布节点。`document_id` / `clip_id` / `media_id` 按节点类型择一使用：

    - text  -> document_id
    - shot  -> clip_id
    - image -> media_id（指向 media_nodes.id，方案或结果节点）
    - video -> media_id（指向 media_nodes.id，方案或结果节点）
    """

    __tablename__ = "canvas_nodes"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    episode_id: str = Field(foreign_key="episodes.id", index=True, max_length=26)
    node_type: CanvasNodeType = enum_field(CanvasNodeType, CanvasNodeType.text, length=16)
    document_id: Optional[str] = Field(default=None, max_length=26, index=True)
    clip_id: Optional[str] = Field(default=None, max_length=26, index=True)
    media_id: Optional[str] = Field(default=None, max_length=26, index=True)
    parent_node_id: Optional[str] = Field(default=None, max_length=26, index=True)
    label: str = Field(default="", max_length=200)
    # 屏蔽只影响当前节点，不级联下游，不删除内容与媒体
    hidden: bool = Field(default=False, nullable=False)
    # 内容指纹：内容变化才变，布局变化不影响
    content_fingerprint: str = Field(default="", max_length=128)


class CanvasNodeLayout(TimestampMixin, table=True):
    __tablename__ = "canvas_node_layout"
    __table_args__ = (UniqueConstraint("node_id", name="uq_canvas_layout_node"),)

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    node_id: str = Field(foreign_key="canvas_nodes.id", index=True, max_length=26)
    x: float = Field(default=0.0)
    y: float = Field(default=0.0)
    width: float = Field(default=320.0)
    height: float = Field(default=220.0)
    z_index: int = Field(default=0)


class DependencyEdge(TimestampMixin, table=True):
    """依赖变化只用于追溯，不自动重跑、不覆盖产物、不自动拼进模型输入。"""

    __tablename__ = "dependency_edges"
    __table_args__ = (
        UniqueConstraint(
            "from_node_id", "to_node_id", "edge_type", name="uq_dependency_edge"
        ),
    )

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    episode_id: str = Field(foreign_key="episodes.id", index=True, max_length=26)
    from_node_id: str = Field(foreign_key="canvas_nodes.id", index=True, max_length=26)
    to_node_id: str = Field(foreign_key="canvas_nodes.id", index=True, max_length=26)
    edge_type: EdgeType = enum_field(EdgeType, EdgeType.derives, length=16)
    note: str = Field(default="", max_length=500)
