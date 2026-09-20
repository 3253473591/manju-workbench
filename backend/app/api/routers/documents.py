"""内容文档路由：草稿、不可变版本与专用动作。"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, DbSession, EditorUser, IdempotencyKey, RequestId
from app.core.errors import AppError
from app.models.enums import DocumentKind, Operation
from app.schemas.content import (
    ClipActionRequest,
    ContentDocumentDetail,
    ContentRevisionList,
    CreateRevisionRequest,
    DocumentActionRequest,
    UpdateDraftRequest,
)
from app.schemas.generation import SubmissionAccepted
from app.services import content_service, generation_service

router = APIRouter(tags=["documents"])

#: (文档类型, action) → 生成操作
DOCUMENT_ACTIONS: dict[tuple[DocumentKind, str], Operation] = {
    (DocumentKind.outline, "optimize"): Operation.outline_optimize,
    (DocumentKind.outline, "generate-setting"): Operation.setting_generate,
    (DocumentKind.outline, "generate-script"): Operation.script_generate,
    (DocumentKind.script, "optimize"): Operation.script_optimize,
    (DocumentKind.script, "split-clips"): Operation.script_split_clips,
    (DocumentKind.script, "plan-characters"): Operation.plan_characters,
    (DocumentKind.setting, "optimize"): Operation.setting_generate,
}


@router.get(
    "/documents/{documentId}",
    response_model=ContentDocumentDetail,
    summary="文档详情（草稿正文 + 当前版本 + 绑定关系）",
)
def get_document(documentId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    return content_service.get_document_detail(db, documentId)


@router.patch(
    "/documents/{documentId}/draft",
    response_model=ContentDocumentDetail,
    summary="保存草稿正文（不产生版本）",
)
def update_draft(
    documentId: str,  # noqa: N803
    payload: UpdateDraftRequest,
    user: EditorUser,
    db: DbSession,
) -> dict:
    return content_service.update_draft(db, documentId, payload.model_dump(exclude_unset=True))


@router.get(
    "/documents/{documentId}/revisions",
    response_model=ContentRevisionList,
    summary="不可变历史版本列表",
)
def list_revisions(documentId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    revisions = content_service.list_revisions(db, documentId)
    return {"items": [content_service.revision_out(r) for r in revisions], "total": len(revisions)}


@router.post(
    "/documents/{documentId}/revisions",
    response_model=ContentDocumentDetail,
    summary="保存为新的不可变版本",
)
def create_revision(
    documentId: str,  # noqa: N803
    payload: CreateRevisionRequest,
    user: EditorUser,
    db: DbSession,
    response: Response,
) -> dict:
    body = payload.model_dump(exclude_unset=True)
    body["createdBy"] = user.id
    detail, created = content_service.create_revision(db, documentId, body)
    # 契约：新增版本 201，未实际修改或切换历史版本 200
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return detail


@router.post(
    "/documents/{documentId}/actions/{action}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SubmissionAccepted,
    summary="专用动作（发送优化、生成设定、生成剧本、拆分 Clip、规划人物立绘）",
)
def run_document_action(
    documentId: str,  # noqa: N803
    action: str,
    payload: DocumentActionRequest,
    user: EditorUser,
    db: DbSession,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
) -> dict:
    document = content_service.get_document_or_404(db, documentId)
    operation = DOCUMENT_ACTIONS.get((document.kind, action))
    if operation is None:
        raise AppError(
            "INVALID_REQUEST",
            f"文档类型 {document.kind.value} 不支持动作 {action}",
            details={"supported": sorted(a for k, a in DOCUMENT_ACTIONS if k == document.kind)},
        )
    if action == "generate-script":
        # 设定为空不能继续（PRD §5.2）
        setting = content_service.find_setting_document(db, document.episode_id)
        if setting is None or not (setting.body or "").strip():
            raise AppError("BUSINESS_RULE_VIOLATION", "人物设定为空，不能继续生成剧本")

    submission, _created = generation_service.create_submission(
        db,
        submission_key=idempotency_key,
        operation=operation,
        episode_id=document.episode_id,
        target_type="document",
        target_id=document.id,
        node_id=document.node_id,
        requested_count=payload.count,
        created_by=user.id,
        request_id=request_id,
        payload=payload.model_dump(),
        lock_key=document.node_id,
    )
    return _accepted(submission)


def _accepted(submission) -> dict:  # type: ignore[no-untyped-def]
    payload = generation_service._payload(submission)
    return {
        "submissionId": submission.id,
        "operation": submission.operation.value,
        "logicalStatus": submission.logical_status.value,
        "requestedCount": submission.requested_count,
        "nodeIds": payload.get("producedNodeIds", []),
    }


__all__ = ["ClipActionRequest", "router"]
