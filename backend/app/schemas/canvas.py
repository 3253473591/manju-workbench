"""Canvas 的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel


class LayoutInput(ApiModel):
    x: float
    y: float
    width: float = Field(ge=120, le=2000)
    height: float = Field(ge=80, le=4000)
    zIndex: int = 0


class LayoutOut(ApiModel):
    x: float
    y: float
    width: float
    height: float
    zIndex: int


class LayoutEnvelope(ApiModel):
    nodeId: str
    layout: LayoutOut


class CanvasNodeOut(ApiModel):
    id: str
    episodeId: str
    nodeType: str
    documentId: str | None = None
    clipId: str | None = None
    mediaId: str | None = None
    parentNodeId: str | None = None
    label: str
    hidden: bool
    contentFingerprint: str = ""
    createdAt: str
    updatedAt: str


class CanvasNodeEnvelope(ApiModel):
    node: CanvasNodeOut


class CanvasNodeDetail(ApiModel):
    node: CanvasNodeOut
    layout: LayoutOut | None = None
    document: dict | None = None
    clip: dict | None = None
    mediaNode: dict | None = None


class CanvasItem(ApiModel):
    node: CanvasNodeOut
    layout: LayoutOut


class DependencyEdgeOut(ApiModel):
    id: str
    episodeId: str
    fromNodeId: str
    toNodeId: str
    edgeType: str
    note: str = ""


class DependencyEdgeEnvelope(ApiModel):
    edge: DependencyEdgeOut


class CanvasSnapshot(ApiModel):
    episodeId: str
    nodes: list[CanvasItem] = Field(default_factory=list)
    edges: list[DependencyEdgeOut] = Field(default_factory=list)


class CreateNodeRequest(ApiModel):
    nodeType: str
    label: str = Field(default="", max_length=200)
    documentId: str | None = None
    clipId: str | None = None
    mediaId: str | None = None
    parentNodeId: str | None = None
    layout: LayoutInput | None = None


class UpdateNodeRequest(ApiModel):
    label: str | None = Field(default=None, max_length=200)


class VisibilityRequest(ApiModel):
    hidden: bool


class EdgeInput(ApiModel):
    fromNodeId: str
    toNodeId: str
    edgeType: str = "derives"
    note: str = Field(default="", max_length=500)
