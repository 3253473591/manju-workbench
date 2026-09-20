"""计费路由：预估、明细与项目合计（仅 manager / admin）。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import BillingViewer, DbSession
from app.schemas.billing import BillingRecordPage, BillingSummary, EstimateResult
from app.schemas.generation import GenerateRequest
from app.services import billing_service, project_service

router = APIRouter(tags=["billing"])


@router.get(
    "/projects/{projectId}/billing/summary",
    response_model=BillingSummary,
    summary="项目费用合计",
)
def get_billing_summary(
    projectId: str, viewer: BillingViewer, db: DbSession  # noqa: N803
) -> dict:
    project_service.get_project_or_404(db, projectId)
    return billing_service.project_billing_summary(db, projectId)


@router.get(
    "/projects/{projectId}/billing/records",
    response_model=BillingRecordPage,
    summary="费用明细",
)
def list_billing_records(
    projectId: str,  # noqa: N803
    viewer: BillingViewer,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),  # noqa: N803
) -> dict:
    project_service.get_project_or_404(db, projectId)
    return billing_service.project_billing_records(
        db, projectId, page=page, page_size=pageSize
    )


@router.post("/estimate", response_model=EstimateResult, summary="生成前费用预估")
def estimate(payload: GenerateRequest, viewer: BillingViewer, db: DbSession) -> dict:
    return billing_service.estimate_generation(
        db,
        operation=payload.operation or "text_optimize",
        count=payload.count,
        product_label=payload.productLabel,
        model_config_id=payload.modelConfigId,
        duration_seconds=payload.durationSeconds,
        resolution=payload.resolution,
        image_pixels=payload.imagePixels,
        source_images=payload.sourceImages,
        input_tokens=payload.inputTokens,
        output_tokens=payload.outputTokens,
    )


__all__ = ["router"]
