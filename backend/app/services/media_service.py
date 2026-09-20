"""媒体服务：方案/结果节点、提示词版本、输入快照与视频参考校验。

隐藏边界（PRD §5.4）：`hidden_preset_text`（A）与 `final_input_text` 只存服务端，
本模块的所有 out 函数都不得带出它们。
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from app.core.errors import AppError
from app.core.utils import to_iso, utcnow
from app.models.asset import Asset, AssetReference
from app.models.common import touch
from app.models.content import Clip
from app.models.enums import MediaKind, MediaType, PromptSource
from app.models.media import MediaItem, MediaNode, MediaSnapshot
from app.models.prompt import PromptVersion


# --- 序列化 -------------------------------------------------------------


def media_item_out(item: MediaItem | None) -> dict | None:
    if item is None:
        return None
    thumb_key = None
    from app.services import media_store

    thumb_key = media_store.thumbnail_key_for(item.file_key)
    has_thumb = media_store.path_for(thumb_key).exists()
    return {
        "id": item.id,
        "mediaType": item.media_type.value,
        "fileKey": item.file_key,
        "originalName": item.original_name,
        "mimeType": item.mime_type,
        "sizeBytes": item.size_bytes,
        "sha256": item.sha256,
        "width": item.width,
        "height": item.height,
        "durationMs": item.duration_ms,
        "decodeStatus": item.decode_status.value,
        "fileUrl": f"/api/v1/media/{item.id}/file",
        "thumbnailUrl": f"/api/v1/media/{item.id}/thumbnail" if has_thumb else None,
    }


def media_node_out(db: Session, node: MediaNode) -> dict:
    try:
        params = json.loads(node.params_json or "{}")
    except json.JSONDecodeError:
        params = {}
    return {
        "id": node.id,
        "episodeId": node.episode_id,
        "mediaKind": node.media_kind.value,
        "mediaItemId": node.media_item_id,
        "sourceMediaId": node.source_media_id,
        "promptVersionId": node.prompt_version_id,
        "clipId": node.clip_id,
        "assetId": node.asset_id,
        "targetName": node.target_name,
        "title": node.title,
        "blocked": node.blocked,
        "seed": node.seed,
        "params": params,
    }


def prompt_version_out(version: PromptVersion) -> dict:
    return {
        "id": version.id,
        "nodeId": version.node_id,
        "revisionNo": version.revision_no,
        "visibleText": version.visible_text,
        "hiddenTemplateVersion": version.hidden_template_version,
        # 只暴露 hash 与版本标识，绝不返回 hidden_preset_text / final_input_text
        "finalInputHash": version.final_input_hash,
        "source": version.source.value,
        "createdAt": to_iso(version.created_at),
    }


# --- 查询 ---------------------------------------------------------------


def get_media_node_or_404(db: Session, node_id: str) -> MediaNode:
    node = db.get(MediaNode, node_id)
    if node is None or node.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "媒体节点不存在或已删除")
    return node


def list_prompt_versions(db: Session, node_id: str) -> list[PromptVersion]:
    return list(
        db.exec(
            select(PromptVersion)
            .where(PromptVersion.node_id == node_id)
            .order_by(PromptVersion.revision_no)
        ).all()
    )


def current_prompt_version(db: Session, node: MediaNode) -> PromptVersion | None:
    if node.prompt_version_id:
        version = db.get(PromptVersion, node.prompt_version_id)
        if version is not None:
            return version
    versions = list_prompt_versions(db, node.id)
    return versions[-1] if versions else None


def get_media_node_detail(db: Session, node_id: str) -> dict:
    node = get_media_node_or_404(db, node_id)
    versions = list_prompt_versions(db, node.id)
    current = current_prompt_version(db, node)
    media_item = db.get(MediaItem, node.media_item_id) if node.media_item_id else None
    source_item = db.get(MediaItem, node.source_media_id) if node.source_media_id else None

    reference_images: list[MediaItem] = []
    frozen_clip = None
    source_summary: dict = {}

    if node.media_kind in (MediaKind.video_plan, MediaKind.video_result) and node.clip_id:
        snapshot = latest_snapshot(db, node.id)
        snapshot_data = json.loads(snapshot.snapshot_json) if snapshot else {}
        source_summary["frozenReferenceCount"] = len(snapshot_data.get("references", []))
        source_summary["frozenAt"] = snapshot_data.get("frozenAt")
        for entry in snapshot_data.get("references", []):
            item = db.get(MediaItem, entry.get("mediaItemId"))
            if item is not None:
                reference_images.append(item)

        # 冻结的 Clip 必须来自**输入快照**，不能读实时行：
        # 读实时行会让「改过 Clip 之后旧方案显示新标题」，等于方案节点偷偷读了最新 Clip。
        frozen_clip_data = snapshot_data.get("clip")
        if frozen_clip_data:
            frozen_clip = {
                "id": frozen_clip_data.get("id"),
                "clipNo": frozen_clip_data.get("clipNo"),
                "title": frozen_clip_data.get("title"),
                "durationMs": frozen_clip_data.get("durationMs"),
                "locationName": frozen_clip_data.get("locationName"),
                "status": frozen_clip_data.get("status"),
            }
        else:
            clip = db.get(Clip, node.clip_id)
            if clip is not None:
                frozen_clip = {
                    "id": clip.id,
                    "clipNo": clip.clip_no,
                    "title": clip.title,
                    "durationMs": clip.duration_ms,
                    "locationName": clip.location_name,
                    "status": clip.status,
                }

    # 提示词规范 lint 结果（来源：产线手册的硬规则），前端据此提示用户先改再抽卡
    try:
        node_params = json.loads(node.params_json or "{}")
    except json.JSONDecodeError:
        node_params = {}
    source_summary["promptWarnings"] = node_params.get("promptWarnings", [])

    return {
        "mediaNode": media_node_out(db, node),
        "visiblePrompt": current.visible_text if current else None,
        "promptVersions": [prompt_version_out(v) for v in versions],
        "media": media_item_out(media_item),
        "sourceMedia": media_item_out(source_item),
        "referenceImages": [media_item_out(i) for i in reference_images],
        "frozenClip": frozen_clip,
        "sourceSummary": source_summary,
    }


def latest_snapshot(db: Session, node_id: str) -> MediaSnapshot | None:
    return db.exec(
        select(MediaSnapshot)
        .where(MediaSnapshot.media_node_id == node_id)
        .order_by(MediaSnapshot.created_at.desc())
        .limit(1)
    ).first()


# --- 写入 ---------------------------------------------------------------


def create_plan_node(
    db: Session,
    *,
    episode_id: str,
    media_kind: MediaKind,
    clip_id: str | None = None,
    asset_id: str | None = None,
    source_media_id: str | None = None,
    title: str = "",
    target_name: str = "",
    params: dict | None = None,
) -> MediaNode:
    node = MediaNode(
        episode_id=episode_id,
        media_kind=media_kind,
        clip_id=clip_id,
        asset_id=asset_id,
        source_media_id=source_media_id,
        title=title,
        target_name=target_name,
        params_json=json.dumps(params or {}, ensure_ascii=False),
    )
    db.add(node)
    db.flush()
    return node


def create_result_node(
    db: Session,
    *,
    episode_id: str,
    media_kind: MediaKind,
    clip_id: str | None = None,
    asset_id: str | None = None,
    source_media_id: str | None = None,
    prompt_version_id: str | None = None,
    title: str = "",
    target_name: str = "",
    seed: int = -1,
    params: dict | None = None,
) -> MediaNode:
    node = MediaNode(
        episode_id=episode_id,
        media_kind=media_kind,
        clip_id=clip_id,
        asset_id=asset_id,
        source_media_id=source_media_id,
        prompt_version_id=prompt_version_id,
        title=title,
        target_name=target_name,
        seed=seed,
        params_json=json.dumps(params or {}, ensure_ascii=False),
    )
    db.add(node)
    db.flush()
    return node


def append_prompt_version(
    db: Session,
    node: MediaNode,
    *,
    visible_text: str,
    source: PromptSource = PromptSource.ai,
    hidden_template_version: str = "",
    hidden_preset_text: str = "",
    final_input_text: str = "",
    final_input_hash: str = "",
    created_by: str | None = None,
    make_current: bool = True,
) -> PromptVersion:
    """追加一个提示词版本。

    注意：参数名不能叫 `select`——那会遮蔽 sqlmodel 的 `select`，导致查询直接 TypeError。
    """
    last_no = db.exec(
        select(PromptVersion.revision_no)
        .where(PromptVersion.node_id == node.id)
        .order_by(PromptVersion.revision_no.desc())
        .limit(1)
    ).first()
    version = PromptVersion(
        node_id=node.id,
        revision_no=int(last_no or 0) + 1,
        visible_text=visible_text or "",
        hidden_template_version=hidden_template_version,
        hidden_preset_text=hidden_preset_text,
        final_input_text=final_input_text,
        final_input_hash=final_input_hash,
        source=source,
        created_by=created_by,
    )
    db.add(version)
    db.flush()
    if make_current:
        node.prompt_version_id = version.id
        db.add(node)
        db.flush()
    return version


def update_media_node(db: Session, node_id: str, payload: dict) -> dict:
    node = get_media_node_or_404(db, node_id)
    if payload.get("blocked") is not None:
        # 屏蔽只影响当前节点，不级联下游，不删除媒体
        node.blocked = bool(payload["blocked"])
    if payload.get("targetName") is not None:
        node.target_name = payload["targetName"][:200]
    if payload.get("title") is not None:
        node.title = payload["title"][:200]
    touch(node)
    db.add(node)
    db.commit()
    db.refresh(node)
    return {"mediaNode": media_node_out(db, node)}


def select_prompt_version(db: Session, node_id: str, version_id: str) -> dict:
    node = get_media_node_or_404(db, node_id)
    version = db.get(PromptVersion, version_id)
    if version is None or version.node_id != node.id:
        raise AppError("RESOURCE_NOT_FOUND", "提示词版本不存在")
    node.prompt_version_id = version.id
    db.add(node)
    db.commit()
    return get_media_node_detail(db, node.id)


# --- 视频参考校验 -------------------------------------------------------


def validate_video_references(db: Session, clip: Clip) -> list[dict]:
    """每个所需资产必须**恰有一个**未屏蔽、文件可用的图片结果。

    0 个、多于 1 个、命名不合规或文件不可用，都由代码拦截并点名具体资产，
    绝不调用视频 Agent，也不让 Agent 自己挑候选。
    """
    from app.services import asset_service

    references = list(
        db.exec(
            select(AssetReference)
            .where(AssetReference.clip_id == clip.id)
            .order_by(AssetReference.slot_no)
        ).all()
    )
    required = [r for r in references if r.required]
    if not required:
        raise AppError(
            "ASSET_REFERENCE_INVALID",
            "该 Clip 还没有参考资产，无法生成视频方案",
            details={"clipId": clip.id, "missing": "all"},
        )

    frozen: list[dict] = []
    problems: list[dict] = []
    for reference in required:
        asset = db.get(Asset, reference.asset_id)
        if asset is None or asset.deleted_at is not None:
            problems.append({"assetId": reference.asset_id, "reason": "asset_missing"})
            continue
        usable = asset_service.usable_image_results(db, asset.id)
        if len(usable) != 1:
            problems.append(
                {
                    "assetId": asset.id,
                    "assetName": asset.canonical_name,
                    "variantName": asset.variant_name,
                    "usableResultCount": len(usable),
                    "reason": "need_exactly_one_usable_result",
                }
            )
            continue
        node = usable[0]
        item = db.get(MediaItem, node.media_item_id)
        if item is None or item.media_type != MediaType.image:
            problems.append(
                {"assetId": asset.id, "assetName": asset.canonical_name, "reason": "file_missing"}
            )
            continue
        frozen.append(
            {
                "slotNo": reference.slot_no,
                "assetId": asset.id,
                "assetName": asset.canonical_name,
                "variantName": asset.variant_name,
                "mediaNodeId": node.id,
                "mediaItemId": item.id,
                "fileKey": item.file_key,
                "sha256": item.sha256,
            }
        )

    if problems:
        raise AppError(
            "ASSET_REFERENCE_INVALID",
            "参考资产校验未通过：每个所需资产必须恰有一个未屏蔽且可读取的图片结果",
            details={"clipId": clip.id, "problems": problems},
        )
    return sorted(frozen, key=lambda entry: entry["slotNo"])


def freeze_snapshot(
    db: Session,
    node: MediaNode,
    *,
    payload: dict,
    attempt_id: str | None = None,
) -> MediaSnapshot:
    snapshot = MediaSnapshot(
        media_node_id=node.id,
        attempt_id=attempt_id,
        snapshot_json=json.dumps(payload, ensure_ascii=False, default=str),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def freeze_video_references(db: Session, node: MediaNode, clip: Clip) -> list[dict]:
    """冻结 Clip、参考图片与槽位顺序；方案节点后续只吃这份冻结上下文。"""
    frozen = validate_video_references(db, clip)
    freeze_snapshot(
        db,
        node,
        payload={
            "frozenAt": to_iso(utcnow()),
            "clip": {
                "id": clip.id,
                "clipNo": clip.clip_no,
                "title": clip.title,
                "durationMs": clip.duration_ms,
                "locationName": clip.location_name,
                "body": clip.body,
                "version": clip.version,
            },
            "references": frozen,
        },
    )
    db.flush()
    return frozen
