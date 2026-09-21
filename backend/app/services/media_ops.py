"""媒体类生成任务的编排：图片方案优化、抽卡、立绘联合派生、视频方案与视频抽卡。

隐藏边界：最终生图/生视频输入（`final_input_text`）与隐藏预设 A 只在这里使用与保存，
不会进入任何响应。
"""

from __future__ import annotations

import json
import random

from sqlmodel import Session

from app.core.config import settings
from app.core.errors import AppError
from app.core.prompts import (
    DERIVE_TEMPLATES,
    IMAGE_PRESETS,
    compose_final_input,
    hash_text,
)
from app.models.canvas import CanvasNode
from app.models.enums import (
    AttemptTarget,
    CanvasNodeType,
    DeliveryStatus,
    MediaKind,
    MediaType,
    Operation,
    PhysicalStatus,
    PromptSource,
)
from app.models.generation import GenerationAttempt, GenerationSubmission
from app.models.media import MediaItem, MediaNode
from app.models.model_config import ModelConfig
from app.providers.base import MediaResult, ProviderError
from app.services import (
    canvas_service,
    clip_service,
    generation_service,
    media_service,
    media_store,
)

IMAGE_SIZE_DEFAULT = "1920x1920"
CANVAS_IMAGE_SIZE = (240.0, 300.0)
CANVAS_VIDEO_SIZE = (280.0, 220.0)

#: 官方 Seedance 档位规格（AI漫剧制作工作流执行手册 §3.1 / §4.0）。
#: 真实档位规格未核验前以手册为准，提交前拦截，避免无效计费。
VIDEO_DURATION_RANGE = {
    "seedance-mini": (4, 15),
    "seedance-fast": (4, 15),
    "seedance-2.5": (4, 30),
}
#: 每片段参考图上限（手册 §4.0：≤9 张参考图 + 3 段参考视频）
MAX_REFERENCE_IMAGES = 9

#: 产品标签 → 档位名，用于查时长规格
VIDEO_LABEL_TO_TIER = {
    "mini": "seedance-mini",
    "fast": "seedance-fast",
    "2.5": "seedance-2.5",
}


def video_duration_range(product_label: str) -> tuple[int, int]:
    tier = VIDEO_LABEL_TO_TIER.get(product_label, "seedance-mini")
    return VIDEO_DURATION_RANGE.get(tier, (4, 15))


def _assert_video_request(
    *, product_label: str, duration_seconds: int, reference_count: int
) -> None:
    """提交前校验（PRD §7.1）：不支持就提示用户调整，**不自动丢图或截断**。"""
    tier = VIDEO_LABEL_TO_TIER.get(product_label, "seedance-mini")
    low, high = video_duration_range(product_label)
    if not (low <= duration_seconds <= high):
        raise AppError(
            "MODEL_NOT_AVAILABLE",
            f"{tier} 只支持 {low}–{high} 秒，当前请求 {duration_seconds} 秒",
            details={
                "tier": tier,
                "durationSeconds": duration_seconds,
                "supported": [low, high],
            },
        )
    if reference_count > MAX_REFERENCE_IMAGES:
        raise AppError(
            "ASSET_REFERENCE_INVALID",
            f"参考图 {reference_count} 张，超过每片段上限 {MAX_REFERENCE_IMAGES} 张；"
            "请调整参考资产，不要自动丢弃",
            details={"referenceCount": reference_count, "limit": MAX_REFERENCE_IMAGES},
        )


def _quality_gate_video_prompt(
    plan: MediaNode, *, visible: str, duration_seconds: int, reference_count: int
) -> list[dict]:
    """对生成的视频提示词跑产线规范 lint，并把问题挂到方案节点上。

    手册的经验是「判一版提示词至少要跑 2 发」，所以这里**不拦死**，
    但必须让用户当场看到缺什么，而不是拿钱去试错。
    """
    from app.services import prompt_rules

    report = prompt_rules.check_video_prompt(
        visible, duration_seconds=duration_seconds, reference_count=reference_count
    )
    problems = report.as_list()
    _save_params(plan, {"promptWarnings": problems})
    return problems


# --- 公共 ---------------------------------------------------------------


def _node(db: Session, node_id: str) -> MediaNode:
    return media_service.get_media_node_or_404(db, node_id)


def _params(node: MediaNode) -> dict:
    try:
        return json.loads(node.params_json or "{}")
    except json.JSONDecodeError:
        return {}


def _save_params(node: MediaNode, values: dict) -> None:
    params = _params(node)
    params.update(values)
    node.params_json = json.dumps(params, ensure_ascii=False)


def _preset_key(node: MediaNode) -> str:
    params = _params(node)
    return str(params.get("preset") or _default_preset(node))


def _default_preset(node: MediaNode) -> str:
    if node.media_kind in (MediaKind.video_plan, MediaKind.video_result):
        return ""
    return "portrait"


def _ensure_final_input(db: Session, node: MediaNode) -> str:
    """保证方案节点有一份冻结的最终输入；同版后续抽卡复用，不再优化。"""
    current = media_service.current_prompt_version(db, node)
    if current is None:
        raise AppError("BUSINESS_RULE_VIOLATION", "该节点还没有提示词方案")
    if current.final_input_text:
        return current.final_input_text
    preset_key = _preset_key(node)
    final_input = compose_final_input(preset_key, current.visible_text)
    current.final_input_text = final_input
    current.final_input_hash = hash_text(final_input)
    db.add(current)
    db.flush()
    return final_input


def _source_bytes(db: Session, node: MediaNode) -> tuple[bytes | None, str]:
    if not node.source_media_id:
        return None, "image/png"
    item = db.get(MediaItem, node.source_media_id)
    if item is None:
        return None, "image/png"
    path = media_store.path_for(item.file_key)
    if not path.exists():
        return None, "image/png"
    return path.read_bytes(), item.mime_type or "image/png"


def _attach_result_node(
    db: Session,
    *,
    plan: MediaNode,
    result_node: MediaNode,
    index: int,
) -> str | None:
    plan_canvas = canvas_service.find_media_node(db, plan.id)
    episode_id = plan.episode_id
    node_type = (
        CanvasNodeType.video
        if result_node.media_kind == MediaKind.video_result
        else CanvasNodeType.image
    )
    width, height = (
        CANVAS_VIDEO_SIZE if node_type == CanvasNodeType.video else CANVAS_IMAGE_SIZE
    )
    x, y = canvas_service.suggest_position(
        db,
        episode_id,
        anchor_node_id=plan_canvas.id if plan_canvas else None,
        direction="sibling" if plan.media_kind == MediaKind.image_plan else "downstream",
        index=index,
        width=width,
        height=height,
    )
    canvas_node = canvas_service.create_media_node(
        db,
        episode_id,
        node_type=node_type,
        media_id=result_node.id,
        label=result_node.title or "结果",
        parent_node_id=plan_canvas.id if plan_canvas else None,
        x=x,
        y=y,
        width=width,
        height=height,
    )
    if plan_canvas is not None:
        canvas_service.create_edge(
            db,
            episode_id,
            {
                "fromNodeId": plan_canvas.id,
                "toNodeId": canvas_node.id,
                "edgeType": "derives",
                "note": "抽卡结果",
            },
            # 自动抽卡不受「同级或下一阶段」的手动拖线限制
            enforce_stage_rule=False,
        )
    return canvas_node.id


def _persist_text_attempt(
    db: Session, submission: GenerationSubmission, *, system_key: str, user_prompt: str,
    expect: str, input_snapshot: dict, context: dict, target_type: AttemptTarget, target_id: str,
) -> dict:
    return generation_service.call_text(
        db,
        submission,
        system_key=system_key,
        user_prompt=user_prompt,
        expect=expect,
        input_snapshot=input_snapshot,
        context=context,
        target_type=target_type,
        target_id=target_id,
        model_config_id=_model_config_id(submission),
    )


def _model_config_id(submission: GenerationSubmission) -> str | None:
    value = generation_service._payload(submission).get("modelConfigId")
    return str(value) if value else None


def _product(submission: GenerationSubmission, default: str) -> str:
    value = generation_service._payload(submission).get("productLabel")
    return str(value) if value else default


# --- 图片 ---------------------------------------------------------------


def handle_image_plan_optimize(db: Session, submission: GenerationSubmission) -> None:
    """方案节点【发送】：追加 V2、V3……，不立即出图。"""
    node = _node(db, submission.target_id or "")
    if node.media_kind != MediaKind.image_plan:
        raise AppError("BUSINESS_RULE_VIOLATION", "只有图片方案节点可以优化方案")
    current = media_service.current_prompt_version(db, node)
    payload, attempt = _persist_text_attempt(
        db,
        submission,
        system_key="image_plan_optimize",
        user_prompt=f"【当前描述】\n{current.visible_text if current else ''}\n\n【本次需求】\n"
        f"{generation_service._payload(submission).get('requirement') or '让画面更具体'}",
        expect="visible_text",
        input_snapshot={
            "mediaNodeId": node.id,
            "promptVersionId": current.id if current else None,
            "requirement": generation_service._payload(submission).get("requirement"),
        },
        context={"base": current.visible_text if current else ""},
        target_type=AttemptTarget.media_node,
        target_id=node.id,
    )
    visible = str(payload.get("visibleText") or "").strip()
    if not visible:
        raise AppError("PROVIDER_REJECTED", "模型没有返回可见描述")

    preset_key = _preset_key(node)
    final_input = compose_final_input(preset_key, visible)
    media_service.append_prompt_version(
        db,
        node,
        visible_text=visible,
        source=PromptSource.ai,
        hidden_template_version=settings.APP_MODE,
        hidden_preset_text=IMAGE_PRESETS.get(preset_key, ""),
        final_input_text=final_input,
        final_input_hash=hash_text(final_input),
        created_by=submission.created_by,
    )
    generation_service.mark_delivery_verified(db, attempt)
    generation_service.update_payload(submission, producedNodeIds=[])
    db.commit()


def handle_image_draw(db: Session, submission: GenerationSubmission) -> None:
    """抽卡：N 次创建 N 个独立同级结果节点，不覆盖旧产物。"""
    plan = _node(db, submission.target_id or "")
    if plan.media_kind != MediaKind.image_plan:
        raise AppError("BUSINESS_RULE_VIOLATION", "只有图片方案节点可以抽卡出图")

    request_payload = generation_service._payload(submission)
    count = max(1, min(int(request_payload.get("count") or 1), 8))
    seed = int(request_payload.get("seed", -1))
    if seed >= 0:
        # 固定 seed 单次数量为 1
        count = 1

    final_input = _ensure_final_input(db, plan)
    source_data, source_mime = _source_bytes(db, plan)
    prompt_version = media_service.current_prompt_version(db, plan)
    size = str(_params(plan).get("size") or IMAGE_SIZE_DEFAULT)

    from app.providers import registry

    provider, model_config = registry.resolve_image_provider(
        db, product_label=_product(submission, "lite"), model_config_id=_model_config_id(submission)
    )

    produced: list[str] = []
    for index in range(count):
        attempt_seed = seed if seed >= 0 else random.randint(0, 2**31 - 1)
        attempt = generation_service.start_attempt(
            db,
            submission,
            operation=Operation.image_draw,
            input_snapshot={
                "mediaNodeId": plan.id,
                "promptVersionId": prompt_version.id if prompt_version else None,
                "finalInputHash": hash_text(final_input),
                "sourceMediaId": plan.source_media_id,
                "seed": attempt_seed,
                "size": size,
                "index": index,
                # 结果节点继续迭代时的来源链：来源结果、来源 prompt version 与
                # 冻结参考图快照都在方案 params 上，原样记进 attempt 快照。
                **_lineage_snapshot(plan),
            },
            fingerprint=hash_text(f"{final_input}|{attempt_seed}|{size}"),
            model_config=model_config,
            is_mock=settings.is_mock,
        )
        db.flush()

        try:
            result = provider.generate(
                prompt=final_input,
                size=size,
                count=1,
                seed=attempt_seed,
                source_image=source_data,
                source_mime=source_mime,
                product_label=_product(submission, "lite"),
            )
        except ProviderError as exc:
            generation_service.finish_attempt_failure(db, attempt, code=exc.code, message=exc.message)
            db.commit()
            raise

        item = media_store.register_media_item(
            db,
            data=result.data,
            media_type=MediaType.image,
            suffix=result.suffix,
            original_name="",
            mime_type=result.mime_type,
            created_by=submission.created_by,
            parsed_kind=_preset_key(plan),
            parsed_name=_asset_name(db, plan),
        )
        result_node = media_service.create_result_node(
            db,
            episode_id=plan.episode_id,
            media_kind=MediaKind.image_result,
            asset_id=plan.asset_id,
            source_media_id=plan.source_media_id,
            prompt_version_id=prompt_version.id if prompt_version else None,
            title=f"{plan.title or '图片'} · 结果",
            target_name=plan.target_name,
            seed=attempt_seed,
            params={"size": size, "preset": _preset_key(plan)},
        )
        result_node.media_item_id = item.id
        db.add(result_node)
        db.flush()

        generation_service.finish_attempt_success(
            db,
            attempt,
            provider_request_id=result.provider_request_id,
            raw_meta=result.raw_meta,
            usage=result.usage,
            usage_context={
                # Seedream pro 按**产物实际像素**分档计价，所以用解码后的尺寸，不用请求参数
                "output_pixels": (item.width or 0) * (item.height or 0) or None,
            },
            model_config=model_config,
        )
        generation_service.mark_model_verified(db, attempt, result.actual_model)
        generation_service.record_delivery(
            db,
            attempt,
            file_key=item.file_key,
            sha256=item.sha256,
            decode_status=item.decode_status.value,
            width=item.width,
            height=item.height,
            thumbnail_file_key=media_store.thumbnail_key_for(item.file_key),
            delivery_status=(
                DeliveryStatus.verified
                if item.decode_status.value == "verified"
                else DeliveryStatus.failed
            ),
        )
        generation_service.link_attempt(db, attempt, AttemptTarget.media_node, result_node.id)
        canvas_id = _attach_result_node(db, plan=plan, result_node=result_node, index=index)
        if canvas_id:
            produced.append(canvas_id)
        db.commit()

    _ensure_asset_status(db, plan)
    generation_service.update_payload(submission, producedNodeIds=produced)
    db.commit()


def _asset_name(db: Session, node: MediaNode) -> str:
    if not node.asset_id:
        return ""
    from app.models.asset import Asset

    asset = db.get(Asset, node.asset_id)
    return asset.canonical_name if asset else ""


def _ensure_asset_status(db: Session, node: MediaNode) -> None:
    if not node.asset_id:
        return
    from app.models.asset import Asset
    from app.models.enums import AssetStatus

    asset = db.get(Asset, node.asset_id)
    if asset is not None and asset.status != AssetStatus.active:
        asset.status = AssetStatus.active
        db.add(asset)


def handle_image_derive(db: Session, submission: GenerationSubmission) -> None:
    """立绘联合派生：以当次选中的立绘为源图，固定各生成 1 张，不覆盖旧结果。"""
    portrait = _node(db, submission.target_id or "")
    if portrait.media_kind != MediaKind.image_result:
        raise AppError("BUSINESS_RULE_VIOLATION", "只有立绘结果节点可以联合派生")
    params = _params(portrait)
    if str(params.get("preset") or "") not in {"portrait", ""}:
        raise AppError("BUSINESS_RULE_VIOLATION", "联合派生仅对立绘结果开放")

    # The user explicitly invoked derive on the currently selected result.
    # `source_media_id` is the immutable provenance of a result and must not
    # override that selection (otherwise deriving after a re-draw uses an old
    # image). Keep both IDs in the attempt snapshot below.
    source_item = db.get(MediaItem, portrait.media_item_id) if portrait.media_item_id else None
    if source_item is None and portrait.source_media_id:
        source_item = db.get(MediaItem, portrait.source_media_id)
    if source_item is None:
        raise AppError("BUSINESS_RULE_VIOLATION", "立绘结果缺少可用源图")

    source_path = media_store.path_for(source_item.file_key)
    if not source_path.exists():
        raise AppError("DELIVERY_FAILED", "立绘源图文件不可定位")
    source_data = source_path.read_bytes()
    size = str(params.get("size") or IMAGE_SIZE_DEFAULT)

    from app.providers import registry

    provider, model_config = registry.resolve_image_provider(
        db, product_label=_product(submission, "lite"), model_config_id=_model_config_id(submission)
    )

    produced: list[str] = []
    for index, (kind, template) in enumerate(DERIVE_TEMPLATES.items()):
        attempt = generation_service.start_attempt(
            db,
            submission,
            operation=Operation.image_derive,
            input_snapshot={
                "sourceMediaNodeId": portrait.id,
                "sourceMediaId": source_item.id,
                "provenanceMediaId": portrait.source_media_id,
                "sourceSha256": source_item.sha256,
                "templateKind": kind,
                "templateVersion": "derive-v1",
                "size": size,
                "index": index,
            },
            fingerprint=hash_text(f"{template}|{source_item.sha256}|{kind}"),
            model_config=model_config,
            is_mock=settings.is_mock,
        )
        db.flush()
        try:
            result = provider.generate(
                prompt=template,
                size=size,
                count=1,
                seed=-1,
                source_image=source_data,
                source_mime=source_item.mime_type or "image/png",
                product_label=_product(submission, "lite"),
            )
        except ProviderError as exc:
            generation_service.finish_attempt_failure(db, attempt, code=exc.code, message=exc.message)
            db.commit()
            raise

        item = media_store.register_media_item(
            db,
            data=result.data,
            media_type=MediaType.image,
            suffix=result.suffix,
            mime_type=result.mime_type,
            created_by=submission.created_by,
            parsed_kind=kind,
            parsed_name=_asset_name(db, portrait),
        )
        label = "面部特写" if kind == "face_closeup" else "三视图"
        result_node = media_service.create_result_node(
            db,
            episode_id=portrait.episode_id,
            media_kind=MediaKind.image_result,
            asset_id=portrait.asset_id,
            source_media_id=source_item.id,
            prompt_version_id=portrait.prompt_version_id,
            title=f"{_asset_name(db, portrait) or '立绘'} · {label}",
            target_name=portrait.target_name,
            seed=-1,
            params={"size": size, "preset": kind, "derivedFrom": portrait.id},
        )
        result_node.media_item_id = item.id
        db.add(result_node)
        db.flush()

        generation_service.finish_attempt_success(
            db,
            attempt,
            provider_request_id=result.provider_request_id,
            raw_meta=result.raw_meta,
            usage=result.usage,
            usage_context={
                # Seedream pro 按**产物实际像素**分档计价，所以用解码后的尺寸，不用请求参数
                "output_pixels": (item.width or 0) * (item.height or 0) or None,
            },
            model_config=model_config,
        )
        generation_service.mark_model_verified(db, attempt, result.actual_model)
        generation_service.record_delivery(
            db,
            attempt,
            file_key=item.file_key,
            sha256=item.sha256,
            decode_status=item.decode_status.value,
            width=item.width,
            height=item.height,
            thumbnail_file_key=media_store.thumbnail_key_for(item.file_key),
            delivery_status=(
                DeliveryStatus.verified
                if item.decode_status.value == "verified"
                else DeliveryStatus.failed
            ),
        )
        generation_service.link_attempt(db, attempt, AttemptTarget.media_node, result_node.id)
        canvas_id = _attach_result_node(db, plan=portrait, result_node=result_node, index=index)
        if canvas_id:
            produced.append(canvas_id)
        db.commit()

    generation_service.update_payload(submission, producedNodeIds=produced)
    db.commit()


def _video_attempt_failure(
    db: Session, attempt: GenerationAttempt, exc: ProviderError
) -> None:
    """视频 attempt 失败收敛。

    如果任务**已经提交给供应商**（`provider_task_id` 已落库），失败的只是这次
    等待/下载，不是这次调用。此时绝不能把 attempt 标成 `failed`：那会抹掉唯一的
    恢复线索，让 reconciler 无从复查，用户重试又会新建一次收费任务。
    """
    if attempt.provider_task_id:
        generation_service.finish_attempt_failure(
            db,
            attempt,
            code=exc.code,
            message=exc.message,
            physical_status=PhysicalStatus.unknown,
        )
    else:
        generation_service.finish_attempt_failure(
            db, attempt, code=exc.code, message=exc.message
        )
    db.commit()


def deliver_video_result(
    db: Session,
    submission: GenerationSubmission | None,
    *,
    plan: MediaNode,
    attempt: GenerationAttempt,
    result: MediaResult,
    duration_seconds: int,
    resolution: str,
    index: int,
    model_config: ModelConfig | None = None,
) -> str | None:
    """落地一次视频产物：媒体落盘 → 结果节点 → attempt 交付核验 → 画布连线。

    抽卡与供应商任务恢复共用这一条路径，保证「恢复出来的结果」和「当场拿到的
    结果」在交付核验、费用与画布关系上完全一致。
    """
    context = _frozen_context(db, plan)
    clip_info = context.get("clip", {})
    references = context.get("references", [])
    prompt_version = media_service.current_prompt_version(db, plan)
    prompt_version_id = (
        prompt_version.id
        if prompt_version is not None
        else _snapshot_prompt_version_id(attempt)
    )

    item = media_store.register_media_item(
        db,
        data=result.data,
        media_type=MediaType.video,
        suffix=result.suffix,
        mime_type=result.mime_type,
        created_by=submission.created_by if submission else None,
        parsed_name=clip_info.get("title") or "",
    )
    result_node = media_service.create_result_node(
        db,
        episode_id=plan.episode_id,
        media_kind=MediaKind.video_result,
        clip_id=plan.clip_id,
        source_media_id=None,
        prompt_version_id=prompt_version_id,
        title=f"{clip_info.get('title') or 'Clip'} · 视频结果",
        seed=int(_snapshot_value(attempt, "seed", -1) or -1),
        params={
            "resolution": resolution,
            "durationSeconds": duration_seconds,
            "frozenReferenceCount": len(references),
        },
    )
    result_node.media_item_id = item.id
    db.add(result_node)
    db.flush()

    generation_service.finish_attempt_success(
        db,
        attempt,
        provider_request_id=result.provider_request_id,
        provider_task_id=result.provider_task_id,
        raw_meta=result.raw_meta,
        usage={
            **result.usage,
            "duration_seconds": duration_seconds,
            "reference_images": len(references),
            "resolution": resolution,
        },
        usage_context={
            "resolution": str(resolution),
            # 本版只发参考图，不发参考视频，因此按「不含视频输入」计价
            "includes_video_input": False,
        },
        model_config=model_config,
    )
    generation_service.mark_model_verified(db, attempt, result.actual_model)
    generation_service.record_delivery(
        db,
        attempt,
        file_key=item.file_key,
        sha256=item.sha256,
        decode_status=item.decode_status.value,
        width=item.width,
        height=item.height,
        duration_ms=item.duration_ms,
        thumbnail_file_key=media_store.thumbnail_key_for(item.file_key),
        delivery_status=(
            DeliveryStatus.verified
            if item.decode_status.value == "verified"
            else DeliveryStatus.failed
        ),
    )
    generation_service.link_attempt(db, attempt, AttemptTarget.media_node, result_node.id)
    return _attach_result_node(db, plan=plan, result_node=result_node, index=index)


def _snapshot(attempt: GenerationAttempt) -> dict:
    try:
        return json.loads(attempt.input_snapshot_json or "{}")
    except json.JSONDecodeError:
        return {}


def _snapshot_value(attempt: GenerationAttempt, key: str, default: object = None) -> object:
    return _snapshot(attempt).get(key, default)


def _snapshot_prompt_version_id(attempt: GenerationAttempt) -> str | None:
    value = _snapshot_value(attempt, "promptVersionId")
    return str(value) if value else None


#: 结果节点继续迭代时，方案 params 上携带的来源链字段。
LINEAGE_KEYS = (
    "sourceResultNodeId",
    "sourceMediaItemId",
    "sourcePromptVersionId",
    "sourceSeed",
    "iterationPlanOf",
)


def _lineage_snapshot(plan: MediaNode) -> dict:
    params = _params(plan)
    return {key: params[key] for key in LINEAGE_KEYS if params.get(key) is not None}


# --- 视频 ---------------------------------------------------------------


def _frozen_context(db: Session, plan: MediaNode) -> dict:
    snapshot = media_service.latest_snapshot(db, plan.id)
    if snapshot is None:
        raise AppError("BUSINESS_RULE_VIOLATION", "视频方案节点缺少冻结的输入快照")
    return json.loads(snapshot.snapshot_json)


def handle_video_plan_create(db: Session, submission: GenerationSubmission) -> None:
    """Clip【生成视频】：冻结 Clip 与参考图顺序，生成初始方案；本次不出片。"""
    clip = clip_service.get_clip_or_404(db, submission.target_id or "")
    plan_id = generation_service._payload(submission).get("mediaNodeId")
    plan = _node(db, str(plan_id))

    context = _frozen_context(db, plan)
    references = context.get("references", [])
    reference_lines = [
        f"参考图{index + 1}（{entry.get('assetName')}）：{entry.get('fileKey')}"
        for index, entry in enumerate(references)
    ]
    clip_info = context.get("clip", {})

    payload, attempt = _persist_text_attempt(
        db,
        submission,
        system_key="video_plan_create",
        user_prompt=(
            f"【Clip】{clip_info.get('title')} / 时长 {clip_info.get('durationMs')}ms\n"
            f"【正文】\n{clip_info.get('body')}\n\n【冻结参考图】\n"
            + ("\n".join(reference_lines) or "（无）")
            + f"\n\n【本次需求】\n{generation_service._payload(submission).get('requirement') or '写出图生视频提示词'}"
        ),
        expect="video_prompt",
        input_snapshot={
            "mediaNodeId": plan.id,
            "clipId": clip.id,
            "clipVersion": clip.version,
            "referenceCount": len(references),
            "referenceFileKeys": [entry.get("fileKey") for entry in references],
        },
        context={
            "base": f"{clip_info.get('title')}，{len(references)} 张参考图",
            "clipTitle": clip_info.get("title"),
            "location": clip_info.get("locationName"),
            "durationSeconds": max(1, round(int(clip_info.get("durationMs") or 5000) / 1000)),
            "referenceNames": [entry.get("assetName") for entry in references],
        },
        target_type=AttemptTarget.media_node,
        target_id=plan.id,
    )
    visible = str(payload.get("visibleText") or "").strip()
    if not visible:
        raise AppError("PROVIDER_REJECTED", "模型没有返回视频提示词")

    media_service.append_prompt_version(
        db,
        plan,
        visible_text=visible,
        source=PromptSource.ai,
        hidden_template_version="video-v1",
        final_input_text=visible,
        final_input_hash=hash_text(visible),
        created_by=submission.created_by,
    )
    warnings = _quality_gate_video_prompt(
        plan,
        visible=visible,
        duration_seconds=max(1, round(int(clip_info.get("durationMs") or 5000) / 1000)),
        reference_count=len(references),
    )
    generation_service.mark_delivery_verified(db, attempt)
    generation_service.update_payload(
        submission, producedNodeIds=[], promptWarnings=warnings
    )
    db.commit()


def handle_video_plan_optimize(db: Session, submission: GenerationSubmission) -> None:
    """方案节点【发送】：只吃冻结上下文，不偷偷读取最新 Clip 或重新选图。"""
    plan = _node(db, submission.target_id or "")
    if plan.media_kind != MediaKind.video_plan:
        raise AppError("BUSINESS_RULE_VIOLATION", "只有视频方案节点可以优化方案")
    context = _frozen_context(db, plan)
    clip_info = context.get("clip", {})
    current = media_service.current_prompt_version(db, plan)
    references = context.get("references", [])

    payload, attempt = _persist_text_attempt(
        db,
        submission,
        system_key="video_plan_optimize",
        user_prompt=(
            f"【当前提示词】\n{current.visible_text if current else ''}\n\n"
            f"【冻结的参考图】{len(references)} 张（编号与顺序不可变）\n"
            f"【冻结的 Clip】{clip_info.get('title')}\n\n"
            f"【本次需求】\n{generation_service._payload(submission).get('requirement') or '优化提示词'}"
        ),
        expect="video_prompt",
        input_snapshot={
            "mediaNodeId": plan.id,
            "promptVersionId": current.id if current else None,
            "frozenClipId": clip_info.get("id"),
            "referenceFileKeys": [entry.get("fileKey") for entry in references],
            "requirement": generation_service._payload(submission).get("requirement"),
        },
        context={
            "base": current.visible_text if current else "",
            "clipTitle": clip_info.get("title"),
            "location": clip_info.get("locationName"),
            "durationSeconds": max(1, round(int(clip_info.get("durationMs") or 5000) / 1000)),
            "referenceNames": [entry.get("assetName") for entry in references],
        },
        target_type=AttemptTarget.media_node,
        target_id=plan.id,
    )
    visible = str(payload.get("visibleText") or "").strip()
    if not visible:
        raise AppError("PROVIDER_REJECTED", "模型没有返回视频提示词")

    media_service.append_prompt_version(
        db,
        plan,
        visible_text=visible,
        source=PromptSource.ai,
        hidden_template_version="video-v1",
        final_input_text=visible,
        final_input_hash=hash_text(visible),
        created_by=submission.created_by,
    )
    warnings = _quality_gate_video_prompt(
        plan,
        visible=visible,
        duration_seconds=max(1, round(int(clip_info.get("durationMs") or 5000) / 1000)),
        reference_count=len(references),
    )
    generation_service.mark_delivery_verified(db, attempt)
    generation_service.update_payload(
        submission, producedNodeIds=[], promptWarnings=warnings
    )
    db.commit()


def handle_video_draw(db: Session, submission: GenerationSubmission) -> None:
    """视频抽卡：N 次创建 N 个独立同级结果节点，每个结果只保存一个视频。"""
    plan = _node(db, submission.target_id or "")
    if plan.media_kind != MediaKind.video_plan:
        raise AppError("BUSINESS_RULE_VIOLATION", "只有视频方案节点可以抽卡出片")

    request_payload = generation_service._payload(submission)
    count = max(1, min(int(request_payload.get("count") or 1), 8))
    seed = int(request_payload.get("seed", -1))
    if seed >= 0:
        count = 1

    context = _frozen_context(db, plan)
    clip_info = context.get("clip", {})
    references = context.get("references", [])
    prompt_version = media_service.current_prompt_version(db, plan)
    if prompt_version is None:
        raise AppError("BUSINESS_RULE_VIOLATION", "视频方案节点还没有提示词")

    reference_images: list[bytes] = []
    for entry in references:
        item = db.get(MediaItem, entry.get("mediaItemId"))
        if item is None:
            raise AppError("ASSET_REFERENCE_INVALID", "冻结的参考图已不可用")
        path = media_store.path_for(item.file_key)
        if not path.exists():
            raise AppError("ASSET_REFERENCE_INVALID", "冻结的参考图文件不可定位")
        reference_images.append(path.read_bytes())

    params = _params(plan)
    product_label = _product(submission, "mini")
    duration_seconds = int(
        params.get("durationSeconds")
        or max(1, round(int(clip_info.get("durationMs") or 5000) / 1000))
    )
    resolution = str(params.get("resolution") or "720p")

    # 提交前校验：档位时长与参考图上限。不通过就直接失败，不截断、不丢图、不调用模型。
    _assert_video_request(
        product_label=product_label,
        duration_seconds=duration_seconds,
        reference_count=len(reference_images),
    )

    from app.providers import registry

    produced: list[str] = []
    for index in range(count):
        attempt_holder: dict = {}

        def on_accepted(task_id: str, holder=attempt_holder) -> None:
            # 拿到供应商任务 ID 立刻持久化，响应丢失后仍可恢复查询
            attempt = holder.get("attempt")
            if attempt is not None:
                attempt.provider_task_id = task_id
                from app.models.enums import PhysicalStatus

                attempt.physical_status = PhysicalStatus.accepted
                db.add(attempt)
                db.commit()

        provider, model_config = registry.resolve_video_provider(
            db,
            product_label=product_label,
            model_config_id=_model_config_id(submission),
            on_accepted=on_accepted,
        )
        attempt_seed = seed if seed >= 0 else -1
        attempt = generation_service.start_attempt(
            db,
            submission,
            operation=Operation.video_draw,
            input_snapshot={
                "mediaNodeId": plan.id,
                "promptVersionId": prompt_version.id,
                "finalInputHash": prompt_version.final_input_hash,
                "frozenClipId": clip_info.get("id"),
                "referenceFileKeys": [entry.get("fileKey") for entry in references],
                "referenceSha256": [entry.get("sha256") for entry in references],
                "durationSeconds": duration_seconds,
                "resolution": resolution,
                "seed": attempt_seed,
                "index": index,
                **_lineage_snapshot(plan),
            },
            fingerprint=hash_text(f"{prompt_version.final_input_hash}|{attempt_seed}|{resolution}"),
            model_config=model_config,
            is_mock=settings.is_mock,
        )
        attempt_holder["attempt"] = attempt
        db.flush()

        try:
            result = provider.generate(
                prompt=prompt_version.visible_text,
                reference_images=reference_images,
                duration_seconds=duration_seconds,
                resolution=resolution,
                ratio="16:9",
                seed=attempt_seed,
                product_label=_product(submission, "mini"),
            )
        except ProviderError as exc:
            _video_attempt_failure(db, attempt, exc)
            raise

        canvas_id = deliver_video_result(
            db,
            submission,
            plan=plan,
            attempt=attempt,
            result=result,
            duration_seconds=duration_seconds,
            resolution=resolution,
            index=index,
            model_config=model_config,
        )
        if canvas_id:
            produced.append(canvas_id)
        db.commit()

    generation_service.update_payload(submission, producedNodeIds=produced)
    db.commit()


RUNNERS = {
    Operation.image_plan_optimize: handle_image_plan_optimize,
    Operation.image_draw: handle_image_draw,
    Operation.image_derive: handle_image_derive,
    Operation.video_plan_create: handle_video_plan_create,
    Operation.video_plan_optimize: handle_video_plan_optimize,
    Operation.video_draw: handle_video_draw,
}


def run(db: Session, submission: GenerationSubmission) -> None:
    handler = RUNNERS.get(submission.operation)
    if handler is None:
        raise AppError("INVALID_REQUEST", f"未实现的操作 {submission.operation.value}")
    handler(db, submission)
