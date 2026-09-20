"""媒体路由：方案/结果节点、提示词版本、抽卡、联合派生与文件读取。"""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import FileResponse

from app.api.deps import (
    CurrentUser,
    DbSession,
    EditorUser,
    IdempotencyKey,
    RequestId,
)
from app.core.errors import AppError
from app.models.enums import MediaKind, Operation
from app.schemas.generation import SubmissionAccepted
from app.schemas.media import (
    CreatePromptVersionRequest,
    DrawRequest,
    MediaNodeDetail,
    MediaNodeEnvelope,
    PromptVersionEnvelope,
    PromptVersionList,
    UpdateMediaNodeRequest,
)
from app.services import generation_service, media_service, media_store

router = APIRouter(tags=["media"])


@router.get("/media-nodes/{mediaNodeId}", response_model=MediaNodeDetail, summary="媒体节点详情")
def get_media_node(mediaNodeId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    return media_service.get_media_node_detail(db, mediaNodeId)


@router.patch(
    "/media-nodes/{mediaNodeId}",
    response_model=MediaNodeEnvelope,
    summary="屏蔽 / 解除屏蔽、确认目标名",
)
def update_media_node(
    mediaNodeId: str,  # noqa: N803
    payload: UpdateMediaNodeRequest,
    user: EditorUser,
    db: DbSession,
) -> dict:
    return media_service.update_media_node(
        db, mediaNodeId, payload.model_dump(exclude_unset=True)
    )


@router.get(
    "/media-nodes/{mediaNodeId}/prompt-versions",
    response_model=PromptVersionList,
    summary="提示词版本列表",
)
def list_prompt_versions(
    mediaNodeId: str, user: CurrentUser, db: DbSession  # noqa: N803
) -> dict:
    media_service.get_media_node_or_404(db, mediaNodeId)
    versions = media_service.list_prompt_versions(db, mediaNodeId)
    return {
        "items": [media_service.prompt_version_out(v) for v in versions],
        "total": len(versions),
    }


@router.post(
    "/media-nodes/{mediaNodeId}/prompt-versions",
    status_code=status.HTTP_201_CREATED,
    response_model=PromptVersionEnvelope,
    summary="追加用户修改的 B 版本",
)
def create_prompt_version(
    mediaNodeId: str,  # noqa: N803
    payload: CreatePromptVersionRequest,
    user: EditorUser,
    db: DbSession,
) -> dict:
    node = media_service.get_media_node_or_404(db, mediaNodeId)
    from app.core.prompts import compose_final_input, hash_text
    from app.models.enums import PromptSource

    preset_key = ""
    try:
        import json

        preset_key = str(json.loads(node.params_json or "{}").get("preset") or "")
    except Exception:  # noqa: BLE001
        preset_key = ""

    final_input = compose_final_input(preset_key, payload.visibleText)
    version = media_service.append_prompt_version(
        db,
        node,
        visible_text=payload.visibleText,
        # 用户修改的版本按用户来源标注
        source=PromptSource.user,
        hidden_template_version="preset-v1",
        final_input_text=final_input,
        final_input_hash=hash_text(final_input),
        created_by=user.id,
        make_current=payload.select,
    )
    db.commit()
    db.refresh(version)
    return {"promptVersion": media_service.prompt_version_out(version)}


@router.post(
    "/media-nodes/{mediaNodeId}/prompt-versions/{versionId}/select",
    response_model=MediaNodeDetail,
    summary="选版（面向 B）",
)
def select_prompt_version(
    mediaNodeId: str,  # noqa: N803
    versionId: str,  # noqa: N803
    user: EditorUser,
    db: DbSession,
) -> dict:
    return media_service.select_prompt_version(db, mediaNodeId, versionId)


@router.post(
    "/media/{mediaNodeId}/draw",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SubmissionAccepted,
    summary="抽卡（N 次生成 N 个独立同级结果节点）",
)
def draw_media(
    mediaNodeId: str,  # noqa: N803
    payload: DrawRequest,
    user: EditorUser,
    db: DbSession,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
) -> dict:
    node = media_service.get_media_node_or_404(db, mediaNodeId)
    operation = {
        MediaKind.image_plan: Operation.image_draw,
        MediaKind.video_plan: Operation.video_draw,
    }.get(node.media_kind)
    if operation is None:
        raise AppError(
            "BUSINESS_RULE_VIOLATION",
            "只有方案节点可以抽卡；结果节点请从所属方案或 Clip 重新发起",
            details={"mediaKind": node.media_kind.value},
        )

    count = payload.count
    if payload.seed is not None and payload.seed >= 0:
        # 固定 seed 单次数量为 1
        count = 1

    submission, _created = generation_service.create_submission(
        db,
        submission_key=idempotency_key,
        operation=operation,
        episode_id=node.episode_id,
        target_type="media_node",
        target_id=node.id,
        node_id=node.id,
        requested_count=count,
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


@router.post(
    "/media-nodes/{mediaNodeId}/derive",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SubmissionAccepted,
    summary="立绘联合派生（面部特写 + 三视图）",
)
def derive_media(
    mediaNodeId: str,  # noqa: N803
    user: EditorUser,
    db: DbSession,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
) -> dict:
    node = media_service.get_media_node_or_404(db, mediaNodeId)
    if node.media_kind != MediaKind.image_result:
        raise AppError("BUSINESS_RULE_VIOLATION", "只有立绘结果节点可以联合派生")

    submission, _created = generation_service.create_submission(
        db,
        submission_key=idempotency_key,
        operation=Operation.image_derive,
        episode_id=node.episode_id,
        target_type="media_node",
        target_id=node.id,
        node_id=node.id,
        requested_count=2,
        created_by=user.id,
        request_id=request_id,
        payload={"mediaNodeId": node.id},
        lock_key=node.id,
    )
    return {
        "submissionId": submission.id,
        "operation": submission.operation.value,
        "logicalStatus": submission.logical_status.value,
        "requestedCount": submission.requested_count,
        "nodeIds": [],
    }


@router.get("/media/{mediaItemId}/file", summary="读取媒体原文件")
def get_media_file(mediaItemId: str, db: DbSession) -> FileResponse:  # noqa: N803
    from app.models.media import MediaItem

    item = db.get(MediaItem, mediaItemId)
    if item is None or item.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "媒体不存在")
    path = media_store.path_for(item.file_key)
    if not path.exists():
        raise AppError("RESOURCE_NOT_FOUND", "媒体文件不可定位")
    return FileResponse(path, media_type=item.mime_type or "application/octet-stream")


@router.get("/media/{mediaItemId}/thumbnail", summary="读取缩略图（视频取第一帧）")
def get_media_thumbnail(mediaItemId: str, db: DbSession) -> FileResponse:  # noqa: N803
    from app.models.media import MediaItem

    item = db.get(MediaItem, mediaItemId)
    if item is None or item.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "媒体不存在")
    key = media_store.ensure_thumbnail(item)
    if not key:
        raise AppError("RESOURCE_NOT_FOUND", "缩略图不可用")
    return FileResponse(media_store.path_for(key), media_type="image/jpeg")


__all__ = ["router"]
