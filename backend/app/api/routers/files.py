"""文件导入路由：文本与图片导入。

- 文本：txt / md / docx → 大纲、剧本或分镜。
- 图片：按资产命名规范解析（忽略扩展名、完全匹配、不做语义匹配）；保留原文件并创建节点；
  导入立绘**不自动创建派生占位**。
"""

from __future__ import annotations

import io
import re
import zipfile

from fastapi import APIRouter, File, Form, UploadFile, status

from app.api.deps import DbSession, EditorUser
from app.core.config import settings
from app.core.errors import AppError
from app.models.content import Clip, ContentDocument
from app.models.enums import (
    AssetType,
    CanvasNodeType,
    DocumentKind,
    DocumentStatus,
    MediaKind,
    MediaType,
)
from app.schemas.files import ImportResult
from app.services import (
    asset_service,
    canvas_service,
    clip_service,
    content_service,
    media_service,
    media_store,
    naming,
    project_service,
)

router = APIRouter(tags=["files"])

TEXT_TARGETS = {
    "outline": DocumentKind.outline,
    "script": DocumentKind.script,
    "clip": DocumentKind.clip,
}

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".docx"}

IMAGE_KIND_TO_ASSET_TYPE = {
    "portrait": AssetType.character,
    "three_view": AssetType.character,
    "face_closeup": AssetType.character,
    "scene": AssetType.scene,
    "prop": AssetType.prop,
}


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AppError("UNSUPPORTED_MEDIA_TYPE", "无法识别文本编码，请另存为 UTF-8")


def _read_docx(data: bytes) -> str:
    """不引第三方依赖，直接从 docx 包里抽出正文 XML 的文本。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "ignore")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise AppError("UNSUPPORTED_MEDIA_TYPE", "docx 文件结构无法解析") from exc
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return text.strip()


def _suffix(filename: str) -> str:
    name = (filename or "").replace("\\", "/").split("/")[-1]
    return ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""


@router.post(
    "/episodes/{episodeId}/imports/text",
    status_code=status.HTTP_201_CREATED,
    response_model=ImportResult,
    summary="导入 txt / md / docx 作为大纲、剧本或分镜",
)
def import_text(
    episodeId: str,  # noqa: N803
    user: EditorUser,
    db: DbSession,
    file: UploadFile = File(...),
    target: str = Form(...),
    title: str | None = Form(default=None),
) -> dict:
    project_service.get_episode_or_404(db, episodeId)
    kind = TEXT_TARGETS.get(target)
    if kind is None:
        raise AppError(
            "INVALID_REQUEST",
            "导入目标只能是 outline / script / clip",
            details={"target": target},
        )
    suffix = _suffix(file.filename or "")
    if suffix not in TEXT_SUFFIXES:
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE",
            "只支持 txt / md / docx",
            details={"suffix": suffix},
        )

    data = file.file.read()
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise AppError("FILE_TOO_LARGE", "文件超过上传上限")
    body = _read_docx(data) if suffix == ".docx" else _decode_text(data)

    stem = naming.strip_extension(file.filename or "导入文本")
    doc_title = title or stem or "导入文本"

    if kind == DocumentKind.clip:
        clip = Clip(
            episode_id=episodeId,
            title=doc_title,
            clip_no=_next_clip_no(db, episodeId),
            status="ready",
            body=body,
        )
        db.add(clip)
        db.flush()
        node = canvas_service.ensure_shot_node(db, episodeId, clip)
    else:
        document = content_service.create_document(
            db,
            episode_id=episodeId,
            kind=kind,
            title=doc_title,
        )
        document.body = body
        document.status = DocumentStatus.draft
        node = canvas_service.ensure_text_node(db, episodeId, document)
        document.node_id = node.id
        db.add(document)
        db.add(node)
        db.flush()
        # 导入不伪造生成记录：直接落一个来源为 import 的版本
        from app.models.enums import RevisionSource

        content_service.append_revision(
            db,
            document,
            body=body,
            title=doc_title,
            source=RevisionSource.import_,
            created_by=user.id,
            metadata={"originalName": file.filename},
        )

    canvas_service.place_node(db, node.id, width=420.0, height=320.0)
    db.commit()
    return {
        "nodeId": node.id,
        "kind": kind.value,
        "created": {"title": doc_title, "originalName": file.filename, "target": target},
        "references": [],
        "mediaItem": None,
    }


def _next_clip_no(db, episode_id: str) -> int:  # type: ignore[no-untyped-def]
    clips = clip_service.list_clips(db, episode_id)
    return max((c.clip_no for c in clips), default=0) + 1


@router.post(
    "/episodes/{episodeId}/imports/image",
    status_code=status.HTTP_201_CREATED,
    response_model=ImportResult,
    summary="导入立绘 / 面部特写 / 三视图 / 场景 / 道具图片",
)
def import_image(
    episodeId: str,  # noqa: N803
    user: EditorUser,
    db: DbSession,
    file: UploadFile = File(...),
    imageKind: str | None = Form(default=None),  # noqa: N803
    canonicalName: str | None = Form(default=None),  # noqa: N803
    variantName: str | None = Form(default=None),  # noqa: N803
    clipId: str | None = Form(default=None),  # noqa: N803
) -> dict:
    episode = project_service.get_episode_or_404(db, episodeId)
    suffix = _suffix(file.filename or "")
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE", "只支持 png / jpg / jpeg / webp / bmp", details={"suffix": suffix}
        )

    data = file.file.read()
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise AppError("FILE_TOO_LARGE", "文件超过上传上限")

    parsed = naming.parse_asset_filename(file.filename or "")
    kind = naming.parse_image_kind(imageKind or "") or (parsed.image_kind if parsed else "")
    asset_type = (
        parsed.asset_type
        if parsed is not None
        else IMAGE_KIND_TO_ASSET_TYPE.get(kind, AssetType.character)
    )
    name = (canonicalName or "").strip() or (parsed.canonical_name if parsed else "")
    if not name:
        raise AppError(
            "BUSINESS_RULE_VIOLATION",
            "无法从文件名解析资产名，请按「00人物-张三-冬装-三视图」规范命名或手动指定名称",
            details={"filename": file.filename},
        )
    variant = (variantName or "").strip() or (parsed.variant_name if parsed else "")

    item = media_store.register_media_item(
        db,
        data=data,
        media_type=MediaType.image,
        suffix=suffix,
        original_name=file.filename or "",
        mime_type=file.content_type or "",
        created_by=user.id,
        parsed_kind=kind,
        parsed_name=name,
        parsed_variant=variant,
    )
    if item.decode_status.value != "verified":
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE", "图片无法解码，未入库", details={"filename": file.filename}
        )

    asset, _created = asset_service.find_or_create_asset(
        db,
        project_id=episode.project_id,
        asset_type=asset_type,
        canonical_name=name,
        variant_name=variant,
        image_kind=kind,
    )

    result_node = media_service.create_result_node(
        db,
        episode_id=episodeId,
        media_kind=MediaKind.image_result,
        asset_id=asset.id,
        title=f"{name} · 导入",
        params={"preset": kind or "portrait", "imported": True},
    )
    result_node.media_item_id = item.id
    db.add(result_node)
    db.flush()

    x, y = canvas_service.suggest_position(
        db, episodeId, direction="sibling", width=240.0, height=300.0
    )
    canvas_node = canvas_service.create_media_node(
        db,
        episodeId,
        node_type=CanvasNodeType.image,
        media_id=result_node.id,
        label=result_node.title,
        parent_node_id=None,
        x=x,
        y=y,
        width=240.0,
        height=300.0,
    )

    references = _match_references(db, episodeId, asset, explicit_clip_id=clipId)
    db.commit()
    return {
        "nodeId": canvas_node.id,
        "kind": kind or asset_type.value,
        "created": {
            "assetId": asset.id,
            "assetType": asset_type.value,
            "canonicalName": name,
            "variantName": variant,
            "imageKind": kind,
        },
        "references": references,
        "mediaItem": media_store and media_service.media_item_out(item),
    }


def _match_references(
    db, episode_id: str, asset, *, explicit_clip_id: str | None  # type: ignore[no-untyped-def]
) -> list[dict]:
    """同名匹配：按完全匹配把资产挂到已经引用它的 Clip 上。"""
    clip_ids: list[str] = []
    if explicit_clip_id:
        clip_ids.append(explicit_clip_id)
    else:
        for clip in clip_service.list_clips(db, episode_id):
            try:
                import json

                characters = json.loads(clip.characters_json or "[]")
            except Exception:  # noqa: BLE001
                characters = []
            if asset.asset_type == AssetType.character and asset.canonical_name in characters:
                clip_ids.append(clip.id)
            elif asset.asset_type == AssetType.scene and clip.location_name == asset.canonical_name:
                clip_ids.append(clip.id)

    out: list[dict] = []
    for clip_id in clip_ids:
        added = clip_service.replace_references_for_clip(db, clip_id, [asset.id])
        out.extend(clip_service.reference_out(db, ref) for ref in added)
    return out


__all__ = ["router"]
