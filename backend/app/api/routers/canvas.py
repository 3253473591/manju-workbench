"""画布路由。"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession, EditorUser
from app.schemas.canvas import (
    CanvasNodeDetail,
    CanvasNodeEnvelope,
    CanvasSnapshot,
    CreateNodeRequest,
    DependencyEdgeEnvelope,
    EdgeInput,
    LayoutEnvelope,
    LayoutInput,
    UpdateNodeRequest,
    VisibilityRequest,
)
from app.services import canvas_service, project_service

router = APIRouter(tags=["canvas"])


@router.get(
    "/episodes/{episodeId}/canvas",
    response_model=CanvasSnapshot,
    summary="画布快照（节点 + 布局 + 依赖边）",
)
def get_canvas(episodeId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    project_service.get_episode_or_404(db, episodeId)
    return canvas_service.get_canvas(db, episodeId)


@router.post(
    "/episodes/{episodeId}/nodes",
    status_code=status.HTTP_201_CREATED,
    response_model=CanvasNodeEnvelope,
    summary="手动创建画布节点",
)
def create_node(
    episodeId: str,  # noqa: N803
    payload: CreateNodeRequest,
    user: EditorUser,
    db: DbSession,
) -> dict:
    project_service.get_episode_or_404(db, episodeId)
    return canvas_service.create_node(db, episodeId, payload.model_dump())


@router.get(
    "/canvas/nodes/{nodeId}",
    response_model=CanvasNodeDetail,
    summary="节点详情（按类型展开内容）",
)
def get_node(nodeId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    return canvas_service.get_node_detail(db, nodeId)


@router.patch(
    "/canvas/nodes/{nodeId}",
    response_model=CanvasNodeEnvelope,
    summary="修改节点标签",
)
def update_node(
    nodeId: str, payload: UpdateNodeRequest, user: EditorUser, db: DbSession  # noqa: N803
) -> dict:
    return canvas_service.update_node(db, nodeId, payload.model_dump(exclude_unset=True))


@router.delete(
    "/canvas/nodes/{nodeId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="软删除节点",
)
def delete_node(nodeId: str, user: EditorUser, db: DbSession) -> None:  # noqa: N803
    canvas_service.delete_node(db, nodeId)


@router.patch(
    "/canvas/nodes/{nodeId}/layout",
    response_model=LayoutEnvelope,
    summary="保存节点位置与尺寸",
)
def update_layout(
    nodeId: str, payload: LayoutInput, user: EditorUser, db: DbSession  # noqa: N803
) -> dict:
    return canvas_service.update_layout(db, nodeId, payload.model_dump())


@router.patch(
    "/canvas/nodes/{nodeId}/visibility",
    response_model=CanvasNodeEnvelope,
    summary="屏蔽 / 解除屏蔽节点",
)
def update_visibility(
    nodeId: str,  # noqa: N803
    payload: VisibilityRequest,
    user: EditorUser,
    db: DbSession,
) -> dict:
    return canvas_service.set_visibility(db, nodeId, payload.hidden)


@router.post(
    "/episodes/{episodeId}/edges",
    status_code=status.HTTP_201_CREATED,
    response_model=DependencyEdgeEnvelope,
    summary="建立依赖边",
)
def create_edge(
    episodeId: str, payload: EdgeInput, user: EditorUser, db: DbSession  # noqa: N803
) -> dict:
    project_service.get_episode_or_404(db, episodeId)
    return canvas_service.create_edge(db, episodeId, payload.model_dump())


@router.delete(
    "/canvas/edges/{edgeId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除依赖边",
)
def delete_edge(edgeId: str, user: EditorUser, db: DbSession) -> None:  # noqa: N803
    canvas_service.delete_edge(db, edgeId)
