"""出图端点：`POST /projects/{pid}/images`（人物卡 / 参考图）。

出图是**内容**动作（owner/member 可做），不是 admin 动作。产物走和出片同一条入仓路径：
落 `.gen/` → 建 asset（补宽高与海报）→ 之后槽位指派就能直接引用它。

同步返回而不是排 job：出图 10~30 s，没有"付费点确认"这一步，排 job 只是多一层轮询。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from manju import runtime
from manju.config import Settings
from manju.deps import PathId, current_user, get_session, get_settings, project_for_write
from manju.errors import ApiError, ErrorCode
from manju.models import Asset, Project, User
from manju.providers import ImageResult, ProviderError, make_image_provider
from manju.seed import enrich_asset

router = APIRouter(tags=["images"])


class ImageIn(BaseModel):
    prompt: Annotated[str, Field(min_length=1, max_length=2_000)]
    model: Annotated[str | None, Field(default=None, max_length=64)] = None


class ImageOut(BaseModel):
    asset_id: int
    path: str
    bytes: int
    source: str
    provider_source: str
    deduped: bool
    model: str
    cost: str
    latency_ms: int
    url: str
    thumb_url: str


@router.post("/projects/{project_id}/images", status_code=201)
def generate_image(
    project_id: PathId,
    payload: ImageIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> ImageOut:
    project: Project = project_for_write(session, user, project_id)
    runtime.assert_enabled(session, "image")  # 通道被停用 → 在调上游之前就拒
    provider = make_image_provider(settings)
    try:
        result: ImageResult = provider.image(prompt=payload.prompt, model=payload.model)
    except ProviderError as exc:
        raise ApiError(
            ErrorCode.PROVIDER_UNAVAILABLE, {"message": str(exc), "retryable": exc.retryable}
        ) from exc

    source = settings.media_root / result.path
    if not source.is_file():
        raise ApiError(ErrorCode.ASSET_FETCH_FAILED, {"path": result.path})
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    asset = session.scalar(select(Asset).where(Asset.project_id == project.id, Asset.sha256 == digest))
    deduped = asset is not None
    if asset is None:
        # `asset.source` 只有 gen/upload/seed 三态（`01` §4.5 的 CHECK）；mock 回放的图来自剧集目录，
        # 记 `seed` 才符合数据模型。这次到底是不是真出图，看响应里的 `provider_source`。
        asset_source = "gen" if result.source == "gen" else "seed"
        candidate = Asset(
            project_id=project.id,
            kind="image",
            path=result.path,
            sha256=digest,
            bytes=result.bytes,
            source=asset_source,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        try:
            # "查不到就插"不是原子的：两个并发出图会同时查空、然后一起插，输的那个撞 uk_asset_sha。
            # 用 savepoint 兜住，竞败方回读对手刚插的行 —— 幂等，且不把整个请求回滚掉。
            with session.begin_nested():
                session.add(candidate)
                session.flush()
            asset = candidate
        except IntegrityError:
            asset = session.scalar(select(Asset).where(Asset.project_id == project.id, Asset.sha256 == digest))
            if asset is None:
                raise ApiError(ErrorCode.INTERNAL_STATE, {"reason": "并发入仓失败且回读不到资产"}) from None
            deduped = True  # 竞败方复用了对手那一行，语义上就是去重命中
        else:
            # 真出图同样要补宽高/海报，否则资产库里是一张"没有预览的空壳"
            enrich_asset(asset, settings.media_root)
            session.flush()
    session.commit()
    base = f"/api/v1/assets/{asset.id}/file"
    return ImageOut(
        asset_id=asset.id,
        path=asset.path,
        bytes=asset.bytes,
        source=asset.source,
        # `source` 是资产来源（命中已有资产时可能是 seed/upload），`provider_source` 才是这次是不是真出图
        provider_source=result.source,
        deduped=deduped,
        model=result.model,
        cost=str(result.cost),
        latency_ms=result.latency_ms,
        url=base,
        thumb_url=f"{base}?thumb=1",
    )
