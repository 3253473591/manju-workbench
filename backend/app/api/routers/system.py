"""系统路由：健康检查与运行模式。"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text
from sqlmodel import select

from app import __version__
from app.core.config import settings
from app.db.engine import get_engine
from app.models.enums import ProviderCapability
from app.models.model_config import ModelConfig
from app.api.deps import DbSession

router = APIRouter(tags=["system"])

IMAGE_PRODUCT_LABELS = ["seedream-lite", "seedream-pro"]
VIDEO_PRODUCT_LABELS = ["seedance-mini", "seedance-fast", "seedance-2.5"]


@router.get("/health", summary="健康检查", openapi_extra={"security": []})
def get_health(db: DbSession) -> dict:
    database = "up"
    try:
        db.exec(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - 探活失败就是 down
        database = "down"
    return {
        "status": "ok",
        "mode": "real" if not settings.is_mock else "mock",
        "version": __version__,
        "database": database,
    }


@router.get("/meta", summary="运行模式与可用模型档位", openapi_extra={"security": []})
def get_meta(db: DbSession) -> dict:
    configs = list(db.exec(select(ModelConfig)).all())

    def labels(capability: ProviderCapability) -> list[str]:
        return [c.product_label for c in configs if c.capability == capability and c.enabled]

    verified = [c for c in configs if c.verified_at is not None]
    image_labels = labels(ProviderCapability.image)
    video_labels = labels(ProviderCapability.video)

    return {
        "mode": "real" if not settings.is_mock else "mock",
        # 只有真实调用核验过的档位才算可用；未核验一律 false
        "realAvailable": bool(verified) and not settings.is_mock,
        "imageProducts": [
            "lite" if label.endswith("lite") else "pro"
            for label in IMAGE_PRODUCT_LABELS
            if label in image_labels
        ],
        "videoProducts": [
            label.replace("seedance-", "")
            for label in VIDEO_PRODUCT_LABELS
            if label in video_labels
        ],
    }
