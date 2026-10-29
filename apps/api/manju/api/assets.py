"""资产元信息、取图与删除。

`asset` 只存相对路径（`01` §4.5），二进制在 `MEDIA_ROOT` 下。取图必须过两道：
权限（项目可读）+ 路径收敛（`realpath` 后仍在 `MEDIA_ROOT` 内）—— 后者是唯一的路径遍历面。

缩略图：`thumb_path` 有就用，没有就回落到原图并在响应头标 `X-Thumb-Fallback: 1`，
让前端知道"这张是全尺寸"，而不是拿到一个 404。

删除：**不做级联**。被片段槽位或某次 take 引用时直接 `409` —— 悬空槽位比"删不掉"更麻烦。
物理文件默认不删（种子素材是用户自己的原始素材，删了没有第二份）；
`?purge=1` 只清 `.gen/` 下我们自己生成的派生文件。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.deps import Paging, PathId, current_user, cursor_page, get_session, get_settings, paging, project_for_read, project_for_write
from manju.errors import ApiError, ErrorCode
from manju.models import Asset, Clip, Episode, Revision, User
from manju.schemas import AssetOut, Page

router = APIRouter(tags=["assets"])

GENERATED_DIR = ".gen"  # 只有这个目录下的是我们自己生成的产物


def _asset_or_404(session: Session, user: User, asset_id: int) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    project_for_read(session, user, asset.project_id)
    return asset


def _asset_out(asset: Asset) -> AssetOut:
    base = f"/api/v1/assets/{asset.id}/file"
    return AssetOut(
        id=asset.id,
        project_id=asset.project_id,
        kind=asset.kind,
        path=asset.path,
        thumb_path=asset.thumb_path,
        bytes=asset.bytes,
        w=asset.w,
        h=asset.h,
        thumb_w=asset.thumb_w,
        thumb_h=asset.thumb_h,
        dur_s=asset.dur_s,
        source=asset.source,
        created_at=asset.created_at,
        url=base,
        thumb_url=f"{base}?thumb=1",
    )


def _resolve(settings: Settings, relative: str) -> Path:
    root = settings.media_root.resolve()
    target = (root / relative).resolve()
    if root != target and root not in target.parents:
        raise ApiError(ErrorCode.INTERNAL, {"reason": "asset 越出 MEDIA_ROOT"})
    return target


@router.get("/assets")
def list_assets(
    project_id: Annotated[int, Query(ge=1)],
    kind: Literal["image", "video", "audio"] | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    page: Paging = Depends(paging),
) -> Page[AssetOut]:
    """项目内的资产列表。`project_id` 必填：跨项目混列既没有 UI 也没有权限语义。"""
    project_for_read(session, user, project_id)
    stmt = select(Asset).where(Asset.project_id == project_id)
    if kind is not None:
        stmt = stmt.where(Asset.kind == kind)
    items, next_cursor = cursor_page(session, stmt, Asset, page)
    return Page(items=[_asset_out(item) for item in items], next_cursor=next_cursor)


@router.get("/assets/{asset_id}")
def get_asset(
    asset_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> AssetOut:
    return _asset_out(_asset_or_404(session, user, asset_id))


@router.get("/assets/{asset_id}/file")
def get_asset_file(
    asset_id: PathId,
    thumb: bool = Query(False),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    asset = _asset_or_404(session, user, asset_id)
    relative = asset.thumb_path if thumb and asset.thumb_path else asset.path
    path = _resolve(settings, relative)
    if not path.is_file():
        raise ApiError(ErrorCode.NOT_FOUND, {"path": relative})
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Cache-Control": "private, max-age=300"}
    if thumb and not asset.thumb_path:
        headers["X-Thumb-Fallback"] = "1"
    return FileResponse(path, media_type=media_type, headers=headers)


def _usages(session: Session, asset: Asset) -> list[dict]:
    """谁在引用它：片段参考槽位 + 某次 take 的产物。

    项目量级很小（≤ 30 片段/集、≤ 50 集），逐条扫 JSON 比写 JSON 路径查询更好读；
    真到大项目再换成 `JSON_CONTAINS` 或引用表。
    """
    usages: list[dict] = []
    clips = session.scalars(
        select(Clip).join(Episode, Episode.id == Clip.episode_id).where(Episode.project_id == asset.project_id)
    ).all()
    for clip in clips:
        for slot in clip.ref_slots_json or []:
            if slot.get("asset_id") == asset.id:
                usages.append(
                    {"clip_id": clip.id, "clip_idx": clip.idx, "episode_id": clip.episode_id, "slot_idx": slot.get("idx")}
                )
    revisions = session.scalars(
        select(Revision).where(Revision.output_asset_id == asset.id)
    ).all()
    for revision in revisions:
        usages.append({"revision_id": revision.id, "target_kind": revision.target_kind, "target_id": revision.target_id})
    return usages


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(
    asset_id: PathId,
    purge: bool = Query(False),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> None:
    """删资产。被引用就 `409` 并列出引用方（`02` §4 的 `CONFLICT_ASSET_REFERENCED`）。

    `purge=1` 额外删物理文件，但**只删 `.gen/` 下的派生文件**：种子/上传素材是用户自己的原始
    素材（`MEDIA_ROOT` 就是他的剧集目录），删了没有第二份，API 不替人做这个决定。
    """
    asset = _asset_or_404(session, user, asset_id)
    project_for_write(session, user, asset.project_id)

    usages = _usages(session, asset)
    if usages:
        raise ApiError(ErrorCode.CONFLICT_ASSET_REFERENCED, {"usages": usages, "count": len(usages)})

    purgeable = Path(asset.path).parts[0] == GENERATED_DIR
    session.delete(asset)
    session.commit()
    if purge and purgeable:
        # 同路径可能还被别的 asset 行引用（同 sha 复用的极端情况），先确认没别人
        others = session.scalar(select(Asset.id).where(Asset.path == asset.path))
        if others is None:
            target = _resolve(settings, asset.path)
            target.unlink(missing_ok=True)
            thumb = _resolve(settings, asset.thumb_path) if asset.thumb_path else None
            if thumb is not None:
                thumb.unlink(missing_ok=True)
