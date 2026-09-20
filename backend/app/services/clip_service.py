"""Clip 服务：Clip、内部镜头、参考资产引用与排序。

叙事顺序不限制生成或播放次序；重新从剧本拆分创建的是**新 Clip 节点**，
旧节点由用户按需屏蔽。
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from app.core.errors import AppError
from app.core.utils import to_iso, utcnow
from app.models.asset import Asset, AssetReference
from app.models.common import touch
from app.models.content import ContentDocument
from app.models.enums import CanvasNodeType, DocumentKind
from app.services import canvas_service

# --- 序列化 -------------------------------------------------------------


def clip_out(clip) -> dict:  # type: ignore[no-untyped-def]
    try:
        characters = json.loads(clip.characters_json or "[]")
    except json.JSONDecodeError:
        characters = []
    return {
        "id": clip.id,
        "episodeId": clip.episode_id,
        "documentId": clip.document_id,
        "sourceRevisionId": clip.source_revision_id,
        "clipNo": clip.clip_no,
        "title": clip.title,
        "durationMs": clip.duration_ms,
        "characters": characters,
        "locationName": clip.location_name,
        "status": clip.status,
        "body": clip.body,
        "version": clip.version,
        "createdAt": to_iso(clip.created_at),
        "updatedAt": to_iso(clip.updated_at),
    }


def shot_out(shot) -> dict:  # type: ignore[no-untyped-def]
    return {
        "id": shot.id,
        "clipId": shot.clip_id,
        "shotNo": shot.shot_no,
        "startMs": shot.start_ms,
        "endMs": shot.end_ms,
        "visualText": shot.visual_text,
        "cameraText": shot.camera_text,
        "dialogueText": shot.dialogue_text,
        "soundText": shot.sound_text,
    }


# --- 查询 ---------------------------------------------------------------


def get_clip_or_404(db: Session, clip_id: str):
    from app.models.content import Clip

    clip = db.get(Clip, clip_id)
    if clip is None or clip.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "Clip 不存在或已删除")
    return clip


def list_clips(db: Session, episode_id: str) -> list:
    from app.models.content import Clip

    return list(
        db.exec(
            select(Clip)
            .where(Clip.episode_id == episode_id, Clip.deleted_at.is_(None))
            .order_by(Clip.clip_no)
        ).all()
    )


def list_shots(db: Session, clip_id: str) -> list:
    from app.models.content import ClipShot

    return list(
        db.exec(
            select(ClipShot)
            .where(ClipShot.clip_id == clip_id)
            .order_by(ClipShot.shot_no)
        ).all()
    )


def list_references(db: Session, clip_id: str) -> list[dict]:
    rows = db.exec(
        select(AssetReference)
        .where(AssetReference.clip_id == clip_id)
        .order_by(AssetReference.slot_no)
    ).all()
    return [reference_out(db, row) for row in rows]


def reference_out(db: Session, reference: AssetReference) -> dict:
    asset = db.get(Asset, reference.asset_id)
    return {
        "id": reference.id,
        "clipId": reference.clip_id,
        "assetId": reference.asset_id,
        "slotNo": reference.slot_no,
        "required": reference.required,
        "source": reference.source,
        "asset": {
            "id": asset.id,
            "projectId": asset.project_id,
            "assetType": asset.asset_type.value,
            "canonicalName": asset.canonical_name,
            "variantName": asset.variant_name,
            "imageKind": asset.image_kind,
            "status": asset.status.value,
            "mediaNodeId": asset.media_node_id,
            "createdAt": to_iso(asset.created_at),
        }
        if asset
        else None,
    }


def get_clip_detail(db: Session, clip_id: str) -> dict:
    clip = get_clip_or_404(db, clip_id)
    return {
        "clip": clip_out(clip),
        "shots": [shot_out(s) for s in list_shots(db, clip.id)],
        "references": list_references(db, clip.id),
    }


# --- 写入 ---------------------------------------------------------------


def _check_version(clip, expected: int | None) -> None:  # type: ignore[no-untyped-def]
    if expected is None:
        return
    if expected != clip.version:
        raise AppError(
            "CONTENT_VERSION_CONFLICT",
            "内容已被更新，请刷新后重试",
            details={"expectedVersion": expected, "actualVersion": clip.version},
        )


def create_clip(db: Session, episode_id: str, payload: dict):
    from app.models.content import Clip

    clip_no = payload.get("clipNo")
    if clip_no is None:
        existing = list_clips(db, episode_id)
        clip_no = (max((c.clip_no for c in existing), default=0)) + 1

    clip = Clip(
        episode_id=episode_id,
        clip_no=clip_no,
        title=payload.get("title") or f"CLIP {clip_no:02d}",
        duration_ms=int(payload.get("durationMs") or 0),
        characters_json=json.dumps(payload.get("characters") or [], ensure_ascii=False),
        location_name=payload.get("locationName") or "",
        body=payload.get("body") or "",
        status=payload.get("status") or "draft",
    )
    db.add(clip)
    db.flush()
    # Clip 是一级节点：创建时同步铺一个 shot 画布节点
    canvas_service.ensure_shot_node(db, episode_id, clip)
    db.commit()
    db.refresh(clip)
    return clip


def update_clip(db: Session, clip_id: str, payload: dict):
    clip = get_clip_or_404(db, clip_id)
    _check_version(clip, payload.get("expectedVersion"))

    if payload.get("clipNo") is not None:
        clip.clip_no = int(payload["clipNo"])
    if payload.get("title") is not None:
        clip.title = payload["title"][:200]
    if payload.get("durationMs") is not None:
        clip.duration_ms = int(payload["durationMs"])
    if payload.get("locationName") is not None:
        clip.location_name = payload["locationName"][:200]
    if payload.get("body") is not None:
        clip.body = payload["body"]
    if payload.get("status") is not None:
        clip.status = str(payload["status"])[:32]
    if payload.get("characters") is not None:
        clip.characters_json = json.dumps(payload["characters"], ensure_ascii=False)

    clip.version += 1
    touch(clip)
    db.add(clip)
    db.commit()
    db.refresh(clip)
    return clip


def delete_clip(db: Session, clip_id: str) -> None:
    clip = get_clip_or_404(db, clip_id)
    clip.deleted_at = utcnow()
    touch(clip)
    db.add(clip)
    db.commit()


def create_shot(db: Session, clip_id: str, payload: dict) -> dict:
    from app.models.content import ClipShot

    clip = get_clip_or_404(db, clip_id)
    shot_no = payload.get("shotNo")
    if shot_no is None:
        shot_no = len(list_shots(db, clip.id)) + 1
    shot = ClipShot(
        clip_id=clip.id,
        shot_no=shot_no,
        start_ms=int(payload.get("startMs") or 0),
        end_ms=int(payload.get("endMs") or 0),
        visual_text=payload.get("visualText") or "",
        camera_text=payload.get("cameraText") or "",
        dialogue_text=payload.get("dialogueText") or "",
        sound_text=payload.get("soundText") or "",
    )
    db.add(shot)
    db.commit()
    db.refresh(shot)
    return shot_out(shot)


def update_shot(db: Session, clip_id: str, shot_id: str, payload: dict) -> dict:
    from app.models.content import ClipShot

    shot = db.get(ClipShot, shot_id)
    if shot is None or shot.clip_id != clip_id:
        raise AppError("RESOURCE_NOT_FOUND", "内部镜头不存在")
    for field, column in (
        ("shotNo", "shot_no"),
        ("startMs", "start_ms"),
        ("endMs", "end_ms"),
        ("visualText", "visual_text"),
        ("cameraText", "camera_text"),
        ("dialogueText", "dialogue_text"),
        ("soundText", "sound_text"),
    ):
        if payload.get(field) is not None:
            setattr(shot, column, payload[field])
    touch(shot)
    db.add(shot)
    db.commit()
    db.refresh(shot)
    return shot_out(shot)


def delete_shot(db: Session, clip_id: str, shot_id: str) -> None:
    from app.models.content import ClipShot

    shot = db.get(ClipShot, shot_id)
    if shot is None or shot.clip_id != clip_id:
        raise AppError("RESOURCE_NOT_FOUND", "内部镜头不存在")
    db.delete(shot)
    db.commit()


def add_reference(db: Session, clip_id: str, payload: dict, *, source: str = "manual") -> dict:
    clip = get_clip_or_404(db, clip_id)
    asset = db.get(Asset, payload["assetId"])
    if asset is None or asset.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "资产不存在")

    existing = db.exec(
        select(AssetReference).where(
            AssetReference.clip_id == clip.id, AssetReference.asset_id == asset.id
        )
    ).first()
    if existing is not None:
        return reference_out(db, existing)

    slot_no = payload.get("slotNo")
    if slot_no is None:
        current = list_references(db, clip.id)
        slot_no = len(current)

    reference = AssetReference(
        clip_id=clip.id,
        asset_id=asset.id,
        slot_no=int(slot_no),
        required=bool(payload.get("required", True)),
        source=source,
    )
    db.add(reference)
    db.commit()
    db.refresh(reference)
    return reference_out(db, reference)


def remove_reference(db: Session, clip_id: str, reference_id: str) -> None:
    """移除引用不删除资产。"""
    reference = db.get(AssetReference, reference_id)
    if reference is None or reference.clip_id != clip_id:
        raise AppError("RESOURCE_NOT_FOUND", "引用不存在")
    db.delete(reference)
    db.commit()


def reorder_clip(db: Session, clip_id: str, clip_no: int):
    clip = get_clip_or_404(db, clip_id)
    clip.clip_no = clip_no
    clip.version += 1
    touch(clip)
    db.add(clip)
    db.commit()
    db.refresh(clip)
    return clip


def replace_references_for_clip(
    db: Session, clip_id: str, asset_ids: list[str], *, source: str = "auto"
) -> list[AssetReference]:
    """按规范名回填：只新增缺失的引用，不清除用户手动引用的其它资产。"""
    added: list[AssetReference] = []
    current = {r.asset_id for r in db.exec(
        select(AssetReference).where(AssetReference.clip_id == clip_id)
    ).all()}
    slot = len(current)
    for asset_id in asset_ids:
        if asset_id in current:
            continue
        reference = AssetReference(
            clip_id=clip_id, asset_id=asset_id, slot_no=slot, required=True, source=source
        )
        db.add(reference)
        added.append(reference)
        current.add(asset_id)
        slot += 1
    db.flush()
    return added


def clip_document(db: Session, clip) -> ContentDocument | None:  # type: ignore[no-untyped-def]
    if not clip.document_id:
        return None
    document = db.get(ContentDocument, clip.document_id)
    if document is None or document.kind != DocumentKind.clip:
        return None
    return document


__all__ = [
    "CanvasNodeType",
    "add_reference",
    "clip_document",
    "clip_out",
    "create_clip",
    "create_shot",
    "delete_clip",
    "delete_shot",
    "get_clip_detail",
    "get_clip_or_404",
    "list_clips",
    "list_references",
    "list_shots",
    "reference_out",
    "remove_reference",
    "reorder_clip",
    "replace_references_for_clip",
    "shot_out",
    "update_clip",
    "update_shot",
]
