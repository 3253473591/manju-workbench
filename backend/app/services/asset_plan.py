"""资产规划确认：查重、规范命名、创建图片方案节点并回填 Clip 引用。

规则（PRD §5.3）：
- 三个入口（人物立绘 / 场景 / 道具）共用查重、规范命名、清单确认与引用回填。
- 确认清单**不直接生图**：命中已有结果或方案时直接复用；未命中才创建图片方案节点及初始 V1。
- 首次角色立绘、场景、道具可走文生图；已有角色新造型从选定角色图发起单源图生图。
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.core.errors import AppError
from app.core.prompts import IMAGE_PRESETS, compose_final_input, hash_text
from app.models.asset import Asset
from app.models.canvas import CanvasNode
from app.models.content import Clip
from app.models.enums import (
    AssetType,
    MediaKind,
    PromptSource,
    RevisionSource,
)
from app.models.media import MediaItem, MediaNode
from app.services import asset_service, canvas_service, clip_service, media_service, naming


def _default_preset(asset: Asset) -> str:
    if asset.image_kind:
        return asset.image_kind
    if asset.asset_type == AssetType.character:
        return "portrait"
    if asset.asset_type == AssetType.scene:
        return "scene"
    return "prop"


def _initial_visible_text(asset: Asset, preset_key: str) -> str:
    display = asset.canonical_name
    if asset.variant_name:
        display = f"{display}（{asset.variant_name}）"
    preset_hint = IMAGE_PRESETS.get(preset_key, "")
    return f"{display}：{preset_hint}" if preset_hint else display


def create_image_plan_for_asset(
    db: Session,
    asset: Asset,
    *,
    image_kind: str = "",
    source_media_id: str | None = None,
    target_name: str = "",
    anchor_node_id: str | None = None,
    created_by: str | None = None,
) -> tuple[MediaNode, bool]:
    """返回 (方案节点, 是否新建)。已有方案直接复用，不重复建节点。"""
    if asset.media_node_id:
        existing = db.get(MediaNode, asset.media_node_id)
        if existing is not None and existing.deleted_at is None:
            return existing, False

    preset_key = naming.parse_image_kind(image_kind) or _default_preset(asset)
    if preset_key not in IMAGE_PRESETS:
        # 模型可能返回 full_body_reference 这类无法识别的图种，回退到按资产类型定的预设，
        # 否则隐藏预设 A 组装不出来，方案描述会缺一截。
        preset_key = _default_preset(asset)
    if image_kind and preset_key:
        asset.image_kind = preset_key

    node = media_service.create_plan_node(
        db,
        episode_id=_episode_for_asset(db, asset),
        media_kind=MediaKind.image_plan,
        asset_id=asset.id,
        source_media_id=source_media_id,
        title=f"{asset.canonical_name} · 图片方案",
        target_name=target_name,
        params={"preset": preset_key, "size": "1920x1920"},
    )

    visible = _initial_visible_text(asset, preset_key)
    final_input = compose_final_input(preset_key, visible)
    media_service.append_prompt_version(
        db,
        node,
        visible_text=visible,
        source=PromptSource.template,
        hidden_template_version="preset-v1",
        hidden_preset_text=IMAGE_PRESETS.get(preset_key, ""),
        final_input_text=final_input,
        final_input_hash=hash_text(final_input),
        created_by=created_by,
    )

    anchor = anchor_node_id
    if anchor is None:
        anchor = _default_anchor(db, asset)
    x, y = canvas_service.suggest_position(
        db,
        node.episode_id,
        anchor_node_id=anchor,
        direction="downstream" if anchor else "sibling",
        index=0,
        width=240.0,
        height=300.0,
    )
    canvas_node = canvas_service.create_media_node(
        db,
        node.episode_id,
        node_type=canvas_service.CanvasNodeType.image,
        media_id=node.id,
        label=node.title,
        parent_node_id=anchor,
        x=x,
        y=y,
        width=240.0,
        height=300.0,
    )
    if anchor:
        canvas_service.create_edge(
            db,
            node.episode_id,
            {
                "fromNodeId": anchor,
                "toNodeId": canvas_node.id,
                "edgeType": "references",
                "note": "资产图片方案",
            },
            # 系统创建的边不受「手动拖线只允许同级或下一阶段」限制：
            # 从大纲/剧本节点直接连到图片方案是正常的业务关系（PRD §7 明确豁免）。
            enforce_stage_rule=False,
        )

    asset.media_node_id = node.id
    db.add(asset)
    db.flush()
    return node, True


def _episode_for_asset(db: Session, asset: Asset) -> str:
    from app.models.project import Episode

    episode = db.exec(
        select(Episode)
        .where(Episode.project_id == asset.project_id)
        .order_by(Episode.number)
    ).first()
    if episode is None:
        raise AppError("RESOURCE_NOT_FOUND", "项目下没有剧集")
    return episode.id


def _default_anchor(db: Session, asset: Asset) -> str | None:
    episode_id = _episode_for_asset(db, asset)
    node = db.exec(
        select(CanvasNode)
        .where(CanvasNode.episode_id == episode_id, CanvasNode.deleted_at.is_(None))
        .order_by(CanvasNode.created_at)
    ).first()
    return node.id if node else None


def _has_result(db: Session, asset_id: str) -> bool:
    return bool(asset_service.usable_image_results(db, asset_id))


def _resolve_scope(
    db: Session, submission_id: str | None, items: list[dict]
) -> dict:
    """确定这次确认属于哪个项目/剧集，以及哪些 Clip 是合法引用目标。

    真实模型并不知道我们内部的 ULID，`clipIds` 经常是 `clip_001` 这类**幻觉值**。
    绝不能拿它去猜项目，更不能找不到就退回到「库里的第一个项目」——那会把资产挂到别的项目上。
    权威范围来自提交本身（submission → episode → 该项目下的全部 Clip）。
    """
    from app.models.generation import GenerationSubmission
    from app.models.project import Episode

    if submission_id:
        submission = db.get(GenerationSubmission, submission_id)
        if submission is None or not submission.episode_id:
            raise AppError("RESOURCE_NOT_FOUND", "来源提交不存在或缺少剧集归属")
        episode = db.get(Episode, submission.episode_id)
        if episode is None:
            raise AppError("RESOURCE_NOT_FOUND", "来源提交的剧集不存在")
        clips = db.exec(
            select(Clip).where(Clip.episode_id == episode.id, Clip.deleted_at.is_(None))
        ).all()
        valid = {clip.id for clip in clips}
        default_clip_ids = [
            str(value) for value in (submission_payload(db, submission).get("clipIds") or [])
            if str(value) in valid
        ]
        if not default_clip_ids:
            default_clip_ids = [clip.id for clip in clips]
        return {
            "project_id": episode.project_id,
            "episode_id": episode.id,
            "clip_ids": valid,
            "default_clip_ids": default_clip_ids,
        }

    # 没有来源提交时，只接受真实存在的 Clip id
    requested = [
        str(value) for raw in items for value in (raw.get("clipIds") or [])
    ]
    clips = [
        clip
        for clip in db.exec(
            select(Clip).where(Clip.id.in_(requested), Clip.deleted_at.is_(None))  # type: ignore[union-attr]
        ).all()
    ] if requested else []
    if not clips:
        raise AppError(
            "BUSINESS_RULE_VIOLATION",
            "无法确定资产归属：请从规划提交确认，或提供属于同一剧集的 Clip",
        )
    episode = db.get(Episode, clips[0].episode_id)
    return {
        "project_id": episode.project_id if episode else "",
        "episode_id": clips[0].episode_id,
        "clip_ids": {clip.id for clip in clips},
        "default_clip_ids": [clip.id for clip in clips],
    }


def submission_payload(db: Session, submission: object) -> dict:
    import json

    try:
        return json.loads(getattr(submission, "payload_json", "{}") or "{}")
    except json.JSONDecodeError:
        return {}


def confirm_plan(
    db: Session,
    *,
    items: list[dict],
    submission_id: str | None,
    created_by: str | None,
) -> dict:
    scope = _resolve_scope(db, submission_id, items)
    created_assets: list[Asset] = []
    created_nodes: list[MediaNode] = []
    reused_nodes: list[MediaNode] = []
    references: list[dict] = []
    dropped_clip_refs: list[str] = []

    for raw in items:
        asset_type = naming.parse_asset_type(raw.get("assetType"))
        if asset_type is None:
            raise AppError(
                "INVALID_REQUEST",
                f"资产类型取值非法：{raw.get('assetType')!r}",
                details={"assetType": str(raw.get("assetType"))},
            )

        canonical = str(raw["canonicalName"]).strip()
        variant = str(raw.get("variantName") or "").strip()
        if not canonical:
            raise AppError("INVALID_REQUEST", "资产规范名不能为空")

        asset, was_created = asset_service.find_or_create_asset(
            db,
            project_id=scope["project_id"],
            asset_type=asset_type,
            canonical_name=canonical,
            variant_name=variant,
            image_kind=naming.parse_image_kind(str(raw.get("imageKind") or "")),
        )
        if was_created:
            created_assets.append(asset)

        requested = [str(value) for value in (raw.get("clipIds") or [])]
        clip_ids = [value for value in requested if value in scope["clip_ids"]]
        dropped_clip_refs.extend(value for value in requested if value not in scope["clip_ids"])
        if not clip_ids:
            # 模型给不出合法 id 时，用提交自带的权威范围回填
            clip_ids = list(scope["default_clip_ids"])

        anchor = _anchor_for_clips(db, clip_ids)

        # 已有可用结果 → 直接复用，不重新生成
        if _has_result(db, asset.id):
            node_id = asset.media_node_id
            if node_id:
                node = db.get(MediaNode, node_id)
                if node is not None:
                    reused_nodes.append(node)
        else:
            node, is_new = create_image_plan_for_asset(
                db,
                asset,
                image_kind=str(raw.get("imageKind") or ""),
                source_media_id=raw.get("sourceMediaId"),
                target_name=str(raw.get("targetName") or ""),
                anchor_node_id=anchor,
                created_by=created_by,
            )
            if is_new:
                created_nodes.append(node)
            else:
                reused_nodes.append(node)

        # 规范资产名回填至 Clip 参考列表
        for clip_id in clip_ids:
            clip = db.get(Clip, clip_id)
            if clip is None or clip.deleted_at is not None:
                continue
            added = clip_service.replace_references_for_clip(db, clip_id, [asset.id])
            references.extend(clip_service.reference_out(db, ref) for ref in added)

    db.commit()
    return {
        "assets": [asset_service.asset_out(a) for a in {a.id: a for a in created_assets}.values()],
        "createdMediaNodes": [
            media_service.media_node_out(db, n) for n in {n.id: n for n in created_nodes}.values()
        ],
        "reused": [
            media_service.media_node_out(db, n) for n in {n.id: n for n in reused_nodes}.values()
        ],
        "references": references,
        "submissionId": submission_id,
        # 模型幻觉出来的 Clip id 会被丢弃，这里如实回报，便于排查提示词问题
        "droppedClipIds": sorted(set(dropped_clip_refs)),
    }


def _anchor_for_clips(db: Session, clip_ids: list[str]) -> str | None:
    for clip_id in clip_ids:
        node = canvas_service.find_clip_node(db, clip_id)
        if node is not None:
            return node.id
    return None


def find_or_create_asset_for_manual(
    db: Session, *, project_id: str, asset_type: AssetType, canonical_name: str, variant_name: str
) -> Asset:
    asset, _ = asset_service.find_or_create_asset(
        db,
        project_id=project_id,
        asset_type=asset_type,
        canonical_name=canonical_name,
        variant_name=variant_name,
    )
    return asset


def imported_image_kind_to_preset(kind: str) -> str:
    return naming.parse_image_kind(kind)


__all__ = [
    "RevisionSource",
    "confirm_plan",
    "create_image_plan_for_asset",
    "find_or_create_asset_for_manual",
    "imported_image_kind_to_preset",
]
