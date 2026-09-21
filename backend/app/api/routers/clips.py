"""Clip 路由：Clip、内部镜头、参考引用、专用动作与视频方案入口。"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession, EditorUser, IdempotencyKey, RequestId
from app.core.errors import AppError
from app.models.enums import (
    CanvasNodeType,
    MediaKind,
    Operation,
)
from app.schemas.clip import (
    AddReferenceRequest,
    ClipDetail,
    ClipEnvelope,
    ClipInput,
    ClipList,
    ClipShotEnvelope,
    ClipShotInput,
    ReorderClipRequest,
    VideoPlanRequest,
)
from app.schemas.content import ClipActionRequest
from app.schemas.generation import VideoPlanAccepted
from app.services import (
    canvas_service,
    clip_service,
    generation_service,
    media_service,
    project_service,
)

router = APIRouter(tags=["clips"])

CLIP_ACTIONS: dict[str, Operation] = {
    "optimize": Operation.clip_optimize,
    "plan-scenes": Operation.plan_scenes,
    "plan-props": Operation.plan_props,
}


@router.get("/episodes/{episodeId}/clips", response_model=ClipList, summary="剧集的 Clip 列表")
def list_clips(episodeId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    project_service.get_episode_or_404(db, episodeId)
    items = clip_service.list_clips(db, episodeId)
    return {"items": [clip_service.clip_out(c) for c in items], "total": len(items)}


@router.post(
    "/episodes/{episodeId}/clips",
    status_code=status.HTTP_201_CREATED,
    response_model=ClipEnvelope,
    summary="手动补建 Clip",
)
def create_clip(
    episodeId: str, payload: ClipInput, user: EditorUser, db: DbSession  # noqa: N803
) -> dict:
    project_service.get_episode_or_404(db, episodeId)
    clip = clip_service.create_clip(db, episodeId, payload.model_dump(exclude_unset=True))
    return {"clip": clip_service.clip_out(clip)}


@router.get("/clips/{clipId}", response_model=ClipDetail, summary="Clip 详情")
def get_clip(clipId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    return clip_service.get_clip_detail(db, clipId)


@router.patch("/clips/{clipId}", response_model=ClipEnvelope, summary="修改 Clip")
def update_clip(
    clipId: str, payload: ClipInput, user: EditorUser, db: DbSession  # noqa: N803
) -> dict:
    clip = clip_service.update_clip(db, clipId, payload.model_dump(exclude_unset=True))
    return {"clip": clip_service.clip_out(clip)}


@router.delete("/clips/{clipId}", status_code=status.HTTP_204_NO_CONTENT, summary="软删除 Clip")
def delete_clip(clipId: str, user: EditorUser, db: DbSession) -> None:  # noqa: N803
    clip_service.delete_clip(db, clipId)


@router.post(
    "/clips/{clipId}/shots",
    status_code=status.HTTP_201_CREATED,
    response_model=ClipShotEnvelope,
    summary="补建内部镜头",
)
def create_shot(
    clipId: str, payload: ClipShotInput, user: EditorUser, db: DbSession  # noqa: N803
) -> dict:
    return {"shot": clip_service.create_shot(db, clipId, payload.model_dump(exclude_unset=True))}


@router.patch(
    "/clips/{clipId}/shots/{shotId}",
    response_model=ClipShotEnvelope,
    summary="修改内部镜头",
)
def update_shot(
    clipId: str,  # noqa: N803
    shotId: str,  # noqa: N803
    payload: ClipShotInput,
    user: EditorUser,
    db: DbSession,
) -> dict:
    return {
        "shot": clip_service.update_shot(
            db, clipId, shotId, payload.model_dump(exclude_unset=True)
        )
    }


@router.delete(
    "/clips/{clipId}/shots/{shotId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除内部镜头",
)
def delete_shot(clipId: str, shotId: str, user: EditorUser, db: DbSession) -> None:  # noqa: N803
    clip_service.delete_shot(db, clipId, shotId)


@router.patch("/clips/{clipId}/order", response_model=ClipEnvelope, summary="调整叙事顺序")
def reorder_clip(
    clipId: str, payload: ReorderClipRequest, user: EditorUser, db: DbSession  # noqa: N803
) -> dict:
    clip = clip_service.reorder_clip(db, clipId, payload.clipNo)
    return {"clip": clip_service.clip_out(clip)}


@router.get(
    "/clips/{clipId}/references",
    response_model=dict,
    summary="Clip 参考资产槽位",
)
def list_references(clipId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    items = clip_service.list_references(db, clipId)
    return {"items": items, "total": len(items)}


@router.post(
    "/clips/{clipId}/references",
    status_code=status.HTTP_201_CREATED,
    response_model=dict,
    summary="添加参考资产引用",
)
def add_reference(
    clipId: str,  # noqa: N803
    payload: AddReferenceRequest,
    user: EditorUser,
    db: DbSession,
) -> dict:
    reference = clip_service.add_reference(
        db, clipId, payload.model_dump(exclude_unset=True)
    )
    return {"reference": reference}


@router.delete(
    "/clips/{clipId}/references/{referenceId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="移除引用（不删除资产）",
)
def remove_reference(
    clipId: str, referenceId: str, user: EditorUser, db: DbSession  # noqa: N803
) -> None:
    clip_service.remove_reference(db, clipId, referenceId)


@router.post(
    "/clips/{clipId}/actions/{action}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=dict,
    summary="Clip 专用动作（发送优化、规划场景、规划道具）",
)
def run_clip_action(
    clipId: str,  # noqa: N803
    action: str,
    payload: ClipActionRequest,
    user: EditorUser,
    db: DbSession,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
) -> dict:
    clip = clip_service.get_clip_or_404(db, clipId)
    operation = CLIP_ACTIONS.get(action)
    if operation is None:
        raise AppError(
            "INVALID_REQUEST",
            f"Clip 不支持动作 {action}",
            details={"supported": sorted(CLIP_ACTIONS)},
        )
    node = canvas_service.find_clip_node(db, clip.id)
    submission, _created = generation_service.create_submission(
        db,
        submission_key=idempotency_key,
        operation=operation,
        episode_id=clip.episode_id,
        target_type="clip",
        target_id=clip.id,
        node_id=node.id if node else None,
        requested_count=payload.count,
        created_by=user.id,
        request_id=request_id,
        payload=payload.model_dump(),
        lock_key=node.id if node else None,
    )
    return {
        "submissionId": submission.id,
        "operation": submission.operation.value,
        "logicalStatus": submission.logical_status.value,
        "requestedCount": submission.requested_count,
        "nodeIds": [],
    }


@router.post(
    "/clips/{clipId}/video-plans",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=VideoPlanAccepted,
    summary="生成视频方案节点",
)
def create_video_plan(
    clipId: str,  # noqa: N803
    payload: VideoPlanRequest,
    user: EditorUser,
    db: DbSession,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
) -> dict:
    clip = clip_service.get_clip_or_404(db, clipId)

    # 幂等必须最先判定：重试要复用原任务，而不是先建一套新方案与新画布节点，
    # 再因为 payload 里的新 plan id 不同而返回 409，留下孤儿节点。
    existing = generation_service.submission_by_key(db, idempotency_key)
    if existing is not None:
        if existing.target_id != clip.id or existing.operation != Operation.video_plan_create:
            raise AppError(
                "IDEMPOTENCY_CONFLICT",
                "相同的 Idempotency-Key 已经用于其他请求",
                details={"submissionKey": idempotency_key},
            )
        payload_existing = generation_service._payload(existing)
        plan_id = payload_existing.get("mediaNodeId")
        plan = db.get(media_service.MediaNode, str(plan_id)) if plan_id else None
        return {
            "submissionId": existing.id,
            "operation": existing.operation.value,
            "logicalStatus": existing.logical_status.value,
            "requestedCount": existing.requested_count,
            "nodeIds": [existing.node_id] if existing.node_id else [],
            "mediaNode": media_service.media_node_out(db, plan) if plan else None,
            "frozenReferenceCount": int(payload_existing.get("frozenReferenceCount") or 0),
        }

    # 稳定锁键：锁的是「Clip 对应的画布节点」，不是本次刚建出来的视频方案节点。
    # 否则重复点击各自拿到新键，节点级生成锁形同虚设。
    clip_node = canvas_service.find_clip_node(db, clip.id)
    lock_key = clip_node.id if clip_node is not None else clip.id
    active = generation_service.active_submission_for_lock(db, lock_key)
    if active is not None:
        raise AppError(
            "GENERATION_IN_PROGRESS",
            "该 Clip 已有未结束的视频方案任务",
            details={"clipId": clip.id, "submissionId": active.id},
        )

    # 先校验：命名不合规、0 个或多于 1 个候选、文件不可用都在这里被点名拦下，
    # 不创建任何节点，也不调用视频 Agent。
    frozen = media_service.validate_video_references(db, clip)

    plan = media_service.create_plan_node(
        db,
        episode_id=clip.episode_id,
        media_kind=MediaKind.video_plan,
        clip_id=clip.id,
        title=f"{clip.title} · 视频方案",
        params={
            "resolution": "720p",
            # 不做静默截断：按 Clip 真实时长记录，超出所选档位范围时由抽卡环节
            # 在提交前报错并提示改用 2.5 档（手册：>15s 只有 2.5 能出）。
            "durationSeconds": max(1, round((clip.duration_ms or 5000) / 1000)),
        },
    )
    media_service.freeze_video_references(db, plan, clip)

    x, y = canvas_service.suggest_position(
        db,
        clip.episode_id,
        anchor_node_id=clip_node.id if clip_node else None,
        direction="downstream",
        index=0,
        width=280.0,
        height=220.0,
    )
    canvas_node = canvas_service.create_media_node(
        db,
        clip.episode_id,
        node_type=CanvasNodeType.video,
        media_id=plan.id,
        label=plan.title,
        parent_node_id=clip_node.id if clip_node else None,
        x=x,
        y=y,
        width=280.0,
        height=220.0,
    )
    if clip_node is not None:
        canvas_service.create_edge(
            db,
            clip.episode_id,
            {
                "fromNodeId": clip_node.id,
                "toNodeId": canvas_node.id,
                "edgeType": "derives",
                "note": "生成视频方案",
            },
            # 专用按钮优于手动拖线规则：Clip → 视频方案允许跨阶段
            enforce_stage_rule=False,
            # 方案、画布节点、依赖边与 submission 必须同事务，失败整体回滚
            commit=False,
        )
    submission_payload = payload.model_dump()
    submission_payload["mediaNodeId"] = plan.id
    submission_payload["frozenReferenceCount"] = len(frozen)
    submission, _created = generation_service.create_submission(
        db,
        submission_key=idempotency_key,
        operation=Operation.video_plan_create,
        episode_id=clip.episode_id,
        target_type="media_node",
        target_id=clip.id,
        node_id=canvas_node.id,
        requested_count=1,
        created_by=user.id,
        request_id=request_id,
        payload=submission_payload,
        lock_key=lock_key,
        commit=False,
    )
    db.commit()
    return {
        "submissionId": submission.id,
        "operation": submission.operation.value,
        "logicalStatus": submission.logical_status.value,
        "requestedCount": submission.requested_count,
        "nodeIds": [canvas_node.id],
        "mediaNode": media_service.media_node_out(db, plan),
        "frozenReferenceCount": len(frozen),
    }


__all__ = ["router"]
