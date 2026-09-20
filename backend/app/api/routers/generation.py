"""生成路由：统一发起入口、提交状态与 attempt 追溯。"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession, EditorUser, IdempotencyKey, RequestId
from app.core.errors import AppError
from app.models.enums import CanvasNodeType, DocumentKind, MediaKind, Operation
from app.models.content import ContentDocument
from app.schemas.generation import (
    AttemptList,
    GenerateRequest,
    SubmissionAccepted,
    SubmissionDetail,
    SubmissionPage,
)
from app.services import (
    canvas_service,
    generation_service,
    media_service,
    project_service,
)

router = APIRouter(tags=["generation"])


@router.post(
    "/nodes/{nodeId}/generate",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SubmissionAccepted,
    summary="从节点发起生成（统一入口）",
)
def generate_from_node(
    nodeId: str,  # noqa: N803
    payload: GenerateRequest,
    user: EditorUser,
    db: DbSession,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
) -> dict:
    node = canvas_service.get_node_or_404(db, nodeId)
    operation, target_id, target_type = _resolve(db, node, payload)
    submission, _created = generation_service.create_submission(
        db,
        submission_key=idempotency_key,
        operation=operation,
        episode_id=node.episode_id,
        target_type=target_type,
        target_id=target_id,
        node_id=node.id,
        requested_count=max(1, payload.count),
        created_by=user.id,
        request_id=request_id,
        payload=payload.model_dump(),
        lock_key=node.id,
    )
    return {
        "submissionId": submission.id,
        "operation": submission.operation.value,
        "logicalStatus": submission.logical_status.value,
        "requestedCount": submission.requested_count,
        "nodeIds": [],
    }


def _resolve(db, node, payload: GenerateRequest) -> tuple[Operation, str, str]:  # type: ignore[no-untyped-def]
    """按节点类型分派到对应的生成操作。"""
    if node.node_type == CanvasNodeType.text and node.document_id:
        document = db.get(ContentDocument, node.document_id)
        if document is None:
            raise AppError("RESOURCE_NOT_FOUND", "文本节点缺少关联文档")
        operation = {
            DocumentKind.outline: Operation.outline_optimize,
            DocumentKind.script: Operation.script_optimize,
            DocumentKind.setting: Operation.setting_generate,
        }.get(document.kind)
        if operation is None:
            raise AppError("BUSINESS_RULE_VIOLATION", "该文本节点没有可用的发送动作")
        return operation, document.id, "document"

    if node.node_type == CanvasNodeType.shot and node.clip_id:
        return Operation.clip_optimize, node.clip_id, "clip"

    if node.media_id:
        media_node = media_service.get_media_node_or_404(db, node.media_id)
        if media_node.media_kind == MediaKind.image_plan:
            return Operation.image_draw, media_node.id, "media_node"
        if media_node.media_kind == MediaKind.video_plan:
            return Operation.video_draw, media_node.id, "media_node"
        if media_node.media_kind == MediaKind.image_result:
            # 已有图片结果【发送】：使用当前图片与需求生成**图生图**方案
            plan = media_service.create_plan_node(
                db,
                episode_id=media_node.episode_id,
                media_kind=MediaKind.image_plan,
                asset_id=media_node.asset_id,
                source_media_id=media_node.media_item_id,
                title=f"{media_node.title} · 图生图方案",
                target_name=media_node.target_name,
                params={"preset": "shot_frame", "size": "1920x1920", "imageToImage": True},
            )
            db.commit()
            return Operation.image_plan_optimize, plan.id, "media_node"
        raise AppError("BUSINESS_RULE_VIOLATION", "结果节点只能从所属方案或 Clip 重新发起")

    raise AppError("BUSINESS_RULE_VIOLATION", "该节点类型暂不支持直接发送生成")


@router.get(
    "/submissions/{submissionId}",
    response_model=SubmissionDetail,
    summary="提交状态（轮询入口）",
)
def get_submission(submissionId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    # user 不返回计费字段；manager/admin 可看脱敏 attempt 元数据
    include_billing = user.role.value in {"manager", "admin"}
    return generation_service.get_submission_detail(
        db, submissionId, include_billing=include_billing
    )


@router.get(
    "/submissions/{submissionId}/attempts",
    response_model=AttemptList,
    summary="该提交下的 attempt",
)
def list_attempts(submissionId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    generation_service.get_submission_or_404(db, submissionId)
    include_billing = user.role.value in {"manager", "admin"}
    attempts = generation_service.list_attempts(db, submissionId)
    return {
        "items": [
            generation_service.attempt_out(db, a, include_billing=include_billing)
            for a in attempts
        ],
        "total": len(attempts),
    }


@router.get(
    "/episodes/{episodeId}/submissions",
    response_model=SubmissionPage,
    summary="剧集的提交历史",
)
def list_episode_submissions(
    episodeId: str,  # noqa: N803
    user: CurrentUser,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),  # noqa: N803
) -> dict:
    project_service.get_episode_or_404(db, episodeId)
    return generation_service.list_submissions(db, episodeId, page=page, page_size=pageSize)


__all__ = ["router"]
