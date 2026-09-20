"""资产路由：项目资产库、资产详情、图片方案与规划确认。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, DbSession, EditorUser
from app.models.enums import AssetType
from app.schemas.asset import (
    AssetDetail,
    AssetPage,
    AssetPlanConfirmRequest,
    AssetPlanConfirmResult,
    UpdateAssetRequest,
)
from app.schemas.media import CreateAssetImagePlanRequest, MediaNodeEnvelope
from app.services import asset_plan, asset_service, media_service, project_service

router = APIRouter(tags=["assets"])


@router.get("/projects/{projectId}/assets", response_model=AssetPage, summary="项目共享资产列表")
def list_assets(
    projectId: str,  # noqa: N803
    user: CurrentUser,
    db: DbSession,
    assetType: str | None = None,  # noqa: N803
    keyword: str | None = None,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),  # noqa: N803
) -> dict:
    project_service.get_project_or_404(db, projectId)
    return asset_service.list_assets(
        db,
        projectId,
        asset_type=assetType,
        keyword=keyword,
        page=page,
        page_size=pageSize,
    )


@router.post(
    "/asset-plans/confirm",
    response_model=AssetPlanConfirmResult,
    summary="确认资产清单（查重、规范命名、回填引用）",
)
def confirm_asset_plan(
    payload: AssetPlanConfirmRequest, user: EditorUser, db: DbSession
) -> dict:
    return asset_plan.confirm_plan(
        db,
        items=[item.model_dump() for item in payload.items],
        submission_id=payload.submissionId,
        created_by=user.id,
    )


@router.get("/assets/{assetId}", response_model=AssetDetail, summary="资产详情与候选结果")
def get_asset(assetId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    return asset_service.get_asset_detail(db, assetId)


@router.patch("/assets/{assetId}", response_model=AssetDetail, summary="修改资产身份")
def update_asset(
    assetId: str,  # noqa: N803
    payload: UpdateAssetRequest,
    user: EditorUser,
    db: DbSession,
) -> dict:
    return asset_service.update_asset(db, assetId, payload.model_dump(exclude_unset=True))


@router.post(
    "/assets/{assetId}/image-plans",
    status_code=status.HTTP_200_OK,
    response_model=MediaNodeEnvelope,
    summary="为资产创建图片方案节点（已有方案则复用）",
)
def create_asset_image_plan(
    assetId: str,  # noqa: N803
    payload: CreateAssetImagePlanRequest,
    user: EditorUser,
    db: DbSession,
    response: Response,
) -> dict:
    asset = asset_service.get_asset_or_404(db, assetId)
    node, created = asset_plan.create_image_plan_for_asset(
        db,
        asset,
        image_kind=payload.imageKind,
        source_media_id=payload.sourceMediaId,
        target_name=payload.targetName,
        created_by=user.id,
    )
    db.commit()
    # 契约：新建 201，复用已有方案 200
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return {"mediaNode": media_service.media_node_out(db, node)}


__all__ = ["AssetType", "router"]
