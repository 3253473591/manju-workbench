"""媒体路由：方案/结果节点、提示词版本、抽卡、联合派生与文件读取。"""

from __future__ import annotations

import json

from fastapi import APIRouter, status
from fastapi.responses import FileResponse
from sqlmodel import select

from app.api.deps import (
    CurrentUser,
    DbSession,
    EditorUser,
    IdempotencyKey,
    RequestId,
)
from app.core.errors import AppError
from app.models.enums import CanvasNodeType, MediaKind, Operation
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
from app.services import canvas_service, generation_service, media_service, media_store

router = APIRouter(tags=["media"])


# --- 结果节点继续迭代 ---------------------------------------------------


def _params(node) -> dict:  # type: ignore[no-untyped-def]
    try:
        return json.loads(node.params_json or "{}")
    except json.JSONDecodeError:
        return {}


def _plan_of_result(db, source):  # type: ignore[no-untyped-def]
    """结果节点所属的方案节点（结果通过 prompt version 指回方案）。"""
    if not source.prompt_version_id:
        return None
    from app.models.media import MediaNode
    from app.models.prompt import PromptVersion

    version = db.get(PromptVersion, source.prompt_version_id)
    if version is None:
        return None
    return db.get(MediaNode, version.node_id)


def _copy_snapshot(db, origin, plan) -> bool:
    """把来源方案的冻结输入原样搬到新方案：历史重抽必须吃同一份冻结快照。"""
    if origin is None or origin.id == plan.id:
        return False
    snapshot = media_service.latest_snapshot(db, origin.id)
    if snapshot is None:
        return False
    media_service.freeze_snapshot(db, plan, payload=json.loads(snapshot.snapshot_json))
    return True


def _existing_iteration_plan(db, source):  # type: ignore[no-untyped-def]
    """已经为这个结果节点建过的迭代方案。

    结果节点只对应一个迭代方案：重复点击复用同一个，不制造孤儿方案节点。
    """
    from app.models.media import MediaNode

    rows = db.exec(
        select(MediaNode).where(
            MediaNode.episode_id == source.episode_id,
            MediaNode.deleted_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    for node in rows:
        if _params(node).get("iterationPlanOf") == source.id:
            return node
    return None


def _followup_plan(db, source, *, user_id: str):  # type: ignore[no-untyped-def]
    """结果节点继续编辑/优化/抽卡时的方案节点。

    - 结果节点只对应**一个**迭代方案：重复点击复用同一个，不制造孤儿节点；
    - 保留来源结果、提示词版本、seed 与冻结参考图快照，历史可追溯。
    """
    existing = _existing_iteration_plan(db, source)
    if existing is not None:
        return existing

    origin = _plan_of_result(db, source)

    plan_kind = (
        MediaKind.image_plan
        if source.media_kind == MediaKind.image_result
        else MediaKind.video_plan
    )
    origin_params = _params(origin) if origin is not None else {}
    if plan_kind == MediaKind.image_plan:
        params = {
            "preset": origin_params.get("preset") or "shot_frame",
            "size": origin_params.get("size") or "1920x1920",
            "imageToImage": True,
        }
    else:
        params = {
            "resolution": origin_params.get("resolution") or "720p",
            "durationSeconds": origin_params.get("durationSeconds") or 5,
        }
    params.update(
        {
            "iterationPlanOf": source.id,
            "sourceResultNodeId": source.id,
            "sourceMediaItemId": source.media_item_id,
            "sourcePromptVersionId": source.prompt_version_id,
            "sourceSeed": source.seed,
        }
    )
    plan = media_service.create_plan_node(
        db,
        episode_id=source.episode_id,
        media_kind=plan_kind,
        clip_id=source.clip_id,
        asset_id=source.asset_id,
        # 图生图的源图 = 当前选中的结果图，不是原始参考图
        source_media_id=source.media_item_id,
        title=f"{source.title} · 继续生成",
        target_name=source.target_name,
        params=params,
    )
    if plan_kind == MediaKind.video_plan:
        if not _copy_snapshot(db, origin, plan) and source.clip_id:
            from app.models.content import Clip

            clip = db.get(Clip, source.clip_id)
            if clip is not None:
                media_service.freeze_video_references(db, plan, clip)
    current = media_service.current_prompt_version(db, source)
    if current is not None:
        media_service.append_prompt_version(
            db,
            plan,
            visible_text=current.visible_text,
            source=current.source,
            hidden_template_version=current.hidden_template_version,
            hidden_preset_text=current.hidden_preset_text,
            final_input_text=current.final_input_text,
            final_input_hash=current.final_input_hash,
            created_by=user_id,
        )
    anchor = canvas_service.find_media_node(db, source.id)
    node_type = (
        CanvasNodeType.image if plan_kind == MediaKind.image_plan else CanvasNodeType.video
    )
    width, height = (240.0, 300.0) if node_type == CanvasNodeType.image else (280.0, 220.0)
    x, y = canvas_service.suggest_position(
        db,
        source.episode_id,
        anchor_node_id=anchor.id if anchor else None,
        direction="downstream",
        index=0,
        width=width,
        height=height,
    )
    canvas = canvas_service.create_media_node(
        db,
        source.episode_id,
        node_type=node_type,
        media_id=plan.id,
        label=plan.title,
        parent_node_id=anchor.id if anchor else None,
        x=x,
        y=y,
        width=width,
        height=height,
    )
    if anchor is not None:
        canvas_service.create_edge(
            db,
            source.episode_id,
            {
                "fromNodeId": anchor.id,
                "toNodeId": canvas.id,
                "edgeType": "derives",
                "note": "结果节点继续生成",
            },
            enforce_stage_rule=False,
            # 方案、画布节点与 submission 同事务
            commit=False,
        )
    return plan


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
    node = media_service.get_media_node_or_404(db, mediaNodeId)
    owner = media_service.prompt_owner_node(db, node)
    versions = media_service.list_prompt_versions(db, owner.id)
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

    # 结果节点的提示词属于它的方案：从结果节点保存 B 时，写入迭代方案（按需建立），
    # 否则这一版对后续抽卡没有任何影响。
    if node.media_kind in (MediaKind.image_result, MediaKind.video_result):
        node = _followup_plan(db, node, user_id=user.id)

    preset_key = ""
    try:
        import json

        preset_key = str(json.loads(node.params_json or "{}").get("preset") or "")
    except Exception:  # noqa: BLE001
        preset_key = ""

    # 「保存用户编辑」只追加一个 prompt version：A+B 的组装是本地模板拼接，
    # 不是模型调用，因此这里**不**创建 attempt。模型优化走 prompt-optimize 接口。
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
    "/media-nodes/{mediaNodeId}/prompt-optimize",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SubmissionAccepted,
    summary="独立执行提示词优化并记录 attempt",
)
def optimize_prompt(
    mediaNodeId: str,  # noqa: N803
    payload: DrawRequest,
    user: EditorUser,
    db: DbSession,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
) -> dict:
    """把「发送优化」与「保存用户编辑」拆成两个操作。

    优化必须落一次独立的 text attempt（含输入快照）；保存 B 只追加 prompt version，
    不产生模型调用。两者不可混为一谈，否则审计上分不清哪一版是模型给的。
    """
    existing = generation_service.submission_by_key(db, idempotency_key)
    if existing is not None:
        return {
            "submissionId": existing.id,
            "operation": existing.operation.value,
            "logicalStatus": existing.logical_status.value,
            "requestedCount": existing.requested_count,
            "nodeIds": [existing.node_id] if existing.node_id else [],
        }
    node = media_service.get_media_node_or_404(db, mediaNodeId)
    if node.media_kind in (MediaKind.image_result, MediaKind.video_result):
        # 结果节点继续优化：复制当前结果为一个迭代方案，优化落在这个方案上。
        node = _followup_plan(db, node, user_id=user.id)
    operation = {
        MediaKind.image_plan: Operation.image_plan_optimize,
        MediaKind.video_plan: Operation.video_plan_optimize,
    }.get(node.media_kind)
    if operation is None:
        raise AppError("BUSINESS_RULE_VIOLATION", "该节点不支持提示词优化")
    submission, _created = generation_service.create_submission(
        db,
        submission_key=idempotency_key,
        operation=operation,
        episode_id=node.episode_id,
        target_type="media_node",
        target_id=node.id,
        node_id=node.id,
        created_by=user.id,
        request_id=request_id,
        payload={
            "mediaNodeId": node.id,
            "requirement": payload.requirement,
            "productLabel": payload.productLabel,
            "modelConfigId": payload.modelConfigId,
        },
        lock_key=node.id,
        commit=False,
    )
    db.commit()
    return {
        "submissionId": submission.id,
        "operation": submission.operation.value,
        "logicalStatus": submission.logical_status.value,
        "requestedCount": submission.requested_count,
        "nodeIds": [node.id],
    }


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
    existing = generation_service.submission_by_key(db, idempotency_key)
    if existing is not None:
        return {
            "submissionId": existing.id,
            "operation": existing.operation.value,
            "logicalStatus": existing.logical_status.value,
            "requestedCount": existing.requested_count,
            "nodeIds": [existing.node_id] if existing.node_id else [],
        }
    node = media_service.get_media_node_or_404(db, mediaNodeId)
    if node.media_kind in (MediaKind.image_result, MediaKind.video_result):
        # 结果节点继续迭代：复制当前结果为一个迭代方案，抽卡仍走方案处理器，
        # 来源结果 / prompt revision / seed / 参考图快照都记在方案的 params 与快照里。
        node = _followup_plan(db, node, user_id=user.id)
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
        commit=False,
    )
    db.commit()
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
