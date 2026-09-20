"""资产服务：项目共享身份、查重、命名与候选结果。

身份唯一键是 (project_id, asset_type, canonical_name, variant_name)；
同名候选各有独立节点和物理文件，不互相覆盖。
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.core.errors import AppError
from app.core.utils import to_iso
from app.models.asset import Asset
from app.models.enums import AssetStatus, AssetType, MediaKind
from app.models.media import MediaNode
from app.services import naming


def asset_out(asset: Asset) -> dict:
    return {
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


def get_asset_or_404(db: Session, asset_id: str) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "资产不存在")
    return asset


def find_asset(
    db: Session,
    project_id: str,
    asset_type: AssetType,
    canonical_name: str,
    variant_name: str = "",
) -> Asset | None:
    """同名查重：忽略扩展名、完全匹配、不做语义匹配。"""
    return db.exec(
        select(Asset).where(
            Asset.project_id == project_id,
            Asset.asset_type == asset_type,
            Asset.canonical_name == canonical_name,
            Asset.variant_name == variant_name,
            Asset.deleted_at.is_(None),
        )
    ).first()


def find_or_create_asset(
    db: Session,
    *,
    project_id: str,
    asset_type: AssetType,
    canonical_name: str,
    variant_name: str = "",
    image_kind: str = "",
) -> tuple[Asset, bool]:
    existing = find_asset(db, project_id, asset_type, canonical_name, variant_name)
    if existing is not None:
        if image_kind and not existing.image_kind:
            existing.image_kind = image_kind
            db.add(existing)
        return existing, False
    asset = Asset(
        project_id=project_id,
        asset_type=asset_type,
        canonical_name=canonical_name,
        variant_name=variant_name,
        image_kind=image_kind,
        status=AssetStatus.planned,
    )
    db.add(asset)
    db.flush()
    return asset, True


def candidate_nodes(db: Session, asset_id: str) -> list[MediaNode]:
    return list(
        db.exec(
            select(MediaNode)
            .where(MediaNode.asset_id == asset_id, MediaNode.deleted_at.is_(None))
            .order_by(MediaNode.created_at)
        ).all()
    )


def usable_image_results(db: Session, asset_id: str) -> list[MediaNode]:
    """恰好一个可用图片结果的判定来源：未屏蔽 + 有产物 + 文件已核验。

    方案节点与未产出节点都不计入候选。
    """
    results = [
        node
        for node in candidate_nodes(db, asset_id)
        if node.media_kind == MediaKind.image_result
        and not node.blocked
        and node.media_item_id
    ]
    from app.models.media import MediaItem
    from app.models.enums import DecodeStatus

    usable: list[MediaNode] = []
    for node in results:
        item = db.get(MediaItem, node.media_item_id)
        if item is None or item.deleted_at is not None:
            continue
        if item.decode_status != DecodeStatus.verified:
            continue
        usable.append(node)
    return usable


def get_asset_detail(db: Session, asset_id: str) -> dict:
    from app.services import media_service

    asset = get_asset_or_404(db, asset_id)
    candidates = [
        media_service.media_node_out(db, node) for node in candidate_nodes(db, asset.id)
    ]
    return {
        "asset": asset_out(asset),
        "candidates": candidates,
        "usableResultCount": len(usable_image_results(db, asset.id)),
    }


def list_assets(
    db: Session,
    project_id: str,
    *,
    asset_type: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    from sqlalchemy import func

    statement = select(Asset).where(
        Asset.project_id == project_id, Asset.deleted_at.is_(None)
    )
    if asset_type:
        try:
            statement = statement.where(Asset.asset_type == AssetType(asset_type))
        except ValueError as exc:
            raise AppError("INVALID_REQUEST", "资产类型取值非法") from exc
    if keyword:
        statement = statement.where(Asset.canonical_name.like(f"%{keyword.strip()}%"))

    count_statement = select(func.count()).select_from(statement.subquery())
    total = db.exec(count_statement).one()
    rows = db.exec(
        statement.order_by(Asset.asset_type, Asset.canonical_name)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [asset_out(a) for a in rows],
        "total": int(total or 0),
        "page": page,
        "pageSize": page_size,
    }


def update_asset(db: Session, asset_id: str, payload: dict) -> dict:
    """改名只影响当前资产身份：退出旧组并归入新组，不改写其它候选与历史快照。"""
    asset = get_asset_or_404(db, asset_id)

    next_name = payload.get("canonicalName") or asset.canonical_name
    next_variant = (
        payload["variantName"] if payload.get("variantName") is not None else asset.variant_name
    )
    if (next_name, next_variant) != (asset.canonical_name, asset.variant_name):
        clash = find_asset(db, asset.project_id, asset.asset_type, next_name, next_variant)
        if clash is not None and clash.id != asset.id:
            raise AppError(
                "BUSINESS_RULE_VIOLATION",
                "同名资产已存在，请换一个名称",
                details={"canonicalName": next_name, "variantName": next_variant},
            )
        asset.canonical_name = next_name
        asset.variant_name = next_variant

    if payload.get("imageKind") is not None:
        asset.image_kind = naming.parse_image_kind(payload["imageKind"])
    if payload.get("status") is not None:
        try:
            asset.status = AssetStatus(payload["status"])
        except ValueError as exc:
            raise AppError("INVALID_REQUEST", "资产状态取值非法") from exc

    db.add(asset)
    db.commit()
    db.refresh(asset)
    return get_asset_detail(db, asset.id)
