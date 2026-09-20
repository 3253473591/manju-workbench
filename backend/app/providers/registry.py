"""供应商注册表：按运行模式与模型配置解析出具体适配器。

mock 不发外部请求；real 只走火山方舟官方接口，且未核验的档位不允许被当作可用。
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from app.core.config import settings
from app.core.errors import AppError
from app.models.enums import ProviderCapability
from app.models.model_config import ModelConfig
from app.providers.ark import ArkImageProvider, ArkTextProvider, ArkVideoProvider
from app.providers.base import ImageProvider, TextProvider, VideoProvider
from app.providers.mock import MockImageProvider, MockTextProvider, MockVideoProvider

DEFAULT_IMAGE_LABEL = "seedream-lite"
DEFAULT_VIDEO_LABEL = "seedance-mini"

#: 产品标签 → 内部档位名（PRD §3 的期望标签）
IMAGE_LABEL_MAP = {"lite": "seedream-lite", "pro": "seedream-pro"}
VIDEO_LABEL_MAP = {
    "mini": "seedance-mini",
    "fast": "seedance-fast",
    "2.5": "seedance-2.5",
}


def _spec(config: ModelConfig | None) -> dict:
    if config is None:
        return {}
    try:
        return json.loads(config.spec_json or "{}")
    except json.JSONDecodeError:
        return {}


def product_label_for(capability: ProviderCapability, label: str | None) -> str | None:
    """把产品期望标签（lite / pro / mini / fast / 2.5）映射成模型配置里的档位名。

    映射只此一处，估计费与真实调用必须走同一套，否则会出现「预估能找到档位、调用找不到」。
    """
    if not label:
        return None
    if capability == ProviderCapability.text:
        # 文本只有一个档位（doubao）；lite / pro / mini 这些标签是图片与视频的概念，
        # 拿它们去筛文本配置只会筛出「找不到模型」。
        return None
    if capability == ProviderCapability.image:
        return IMAGE_LABEL_MAP.get(label.lower(), DEFAULT_IMAGE_LABEL)
    if capability == ProviderCapability.video:
        return VIDEO_LABEL_MAP.get(label.lower(), DEFAULT_VIDEO_LABEL)
    return label


def get_model_config(db: Session, config_id: str) -> ModelConfig:
    config = db.get(ModelConfig, config_id)
    if config is None:
        raise AppError("MODEL_NOT_AVAILABLE", "模型配置不存在")
    return config


def find_model_config(
    db: Session,
    capability: ProviderCapability,
    *,
    product_label: str | None = None,
    config_id: str | None = None,
) -> ModelConfig:
    if config_id:
        config = get_model_config(db, config_id)
        if config.capability != capability:
            raise AppError("INVALID_REQUEST", "所选模型配置的能力与本次任务不匹配")
        if not config.enabled:
            raise AppError("MODEL_NOT_AVAILABLE", "该模型档位已被停用")
        return config

    statement = select(ModelConfig).where(
        ModelConfig.capability == capability, ModelConfig.enabled.is_(True)  # type: ignore[union-attr]
    )
    if product_label:
        statement = statement.where(ModelConfig.product_label == product_label)
    else:
        statement = statement.where(ModelConfig.is_default.is_(True))

    config = db.exec(statement).first()
    if config is None and product_label is None:
        config = db.exec(
            select(ModelConfig).where(
                ModelConfig.capability == capability,
                ModelConfig.enabled.is_(True),  # type: ignore[union-attr]
            )
        ).first()
    if config is None:
        raise AppError(
            "MODEL_NOT_AVAILABLE",
            "没有可用的模型档位，请在模型配置里启用一个",
            details={"capability": capability.value, "productLabel": product_label},
        )
    return config


def resolve_text_provider(
    db: Session, *, model_config_id: str | None = None
) -> tuple[TextProvider, ModelConfig | None]:
    if settings.is_mock:
        return MockTextProvider(), None
    config = find_model_config(
        db, ProviderCapability.text, config_id=model_config_id
    )
    return (
        ArkTextProvider(credential_ref=config.credential_ref, actual_model=config.actual_model),
        config,
    )


def resolve_image_provider(
    db: Session,
    *,
    product_label: str | None = None,
    model_config_id: str | None = None,
) -> tuple[ImageProvider, ModelConfig | None]:
    if settings.is_mock:
        return MockImageProvider(), None
    label = product_label_for(ProviderCapability.image, product_label or "lite")
    config = find_model_config(
        db, ProviderCapability.image, product_label=label, config_id=model_config_id
    )
    spec = _spec(config)
    return (
        ArkImageProvider(
            credential_ref=config.credential_ref,
            endpoint_id=config.endpoint_id,
            min_pixels=int(spec.get("minPixels") or 3686400),
        ),
        config,
    )


def resolve_video_provider(
    db: Session,
    *,
    product_label: str | None = None,
    model_config_id: str | None = None,
    on_accepted=None,  # type: ignore[no-untyped-def]
) -> tuple[VideoProvider, ModelConfig | None]:
    if settings.is_mock:
        return MockVideoProvider(), None
    label = product_label_for(ProviderCapability.video, product_label or "mini")
    config = find_model_config(
        db, ProviderCapability.video, product_label=label, config_id=model_config_id
    )
    return (
        ArkVideoProvider(
            credential_ref=config.credential_ref,
            endpoint_id=config.endpoint_id,
            on_accepted=on_accepted,
        ),
        config,
    )


def assert_real_ready(db: Session) -> None:
    """real 模式下的启动自检：至少要有可用凭证，否则直接报不可用。"""
    if settings.is_mock:
        return
    missing = [
        name
        for name, value in (
            ("ARK_DOUBAO_API_KEY", settings.ARK_DOUBAO_API_KEY),
            ("ARK_SEEDREAM_API_KEY", settings.ARK_SEEDREAM_API_KEY),
        )
        if not value.strip()
    ]
    if missing:
        raise AppError(
            "PROVIDER_UNAVAILABLE",
            "real 模式缺少必要凭证，未核验的档位不会被视为可用",
            details={"missing": missing},
        )


__all__ = [
    "find_model_config",
    "resolve_image_provider",
    "resolve_text_provider",
    "resolve_video_provider",
]
