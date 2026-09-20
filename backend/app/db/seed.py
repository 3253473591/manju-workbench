"""首次启动的数据初始化：管理员账号与模型配置。

模型配置里的 `verified_at` 只有真实调用成功过才写入；未核验的档位不允许被当作可用。
`spec_json` 里的规格来自 API对接文档.md 与 AI漫剧制作工作流执行手册.md 的实测记录；
`spec_json.pricing` 是**用户提供的当前官方报价**（2026-09-20），计价逻辑见
`app/services/pricing.py`——改单价只需要改这里，不用改代码。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from app.core.config import settings
from app.models.enums import ProviderCapability
from app.models.model_config import ModelConfig

#: 单价口径（用户于 2026-09-20 提供的当前官方报价）
PRICING_DOUBAO = {
    "kind": "token_pair",
    "inputPer1M": "3",
    "outputPer1M": "15",
    "currency": "CNY",
    "note": "推理输入 3 元/百万 tokens；推理输出 15 元/百万 tokens",
}
PRICING_SEEDREAM_LITE = {
    "kind": "per_image",
    "perImage": "0.22",
    "currency": "CNY",
    "note": "文生图与图生图均 0.22 元/张",
}
PRICING_SEEDREAM_PRO = {
    "kind": "seedream_pro",
    "inputImagePerImage": "0.02",
    "firstInputImageFree": True,
    "outputImageBase": "0.3",
    "outputImageLarge": "0.6",
    "largePixelThreshold": 2610000,
    "currency": "CNY",
    "note": "输入图 0.02 元/张（首张免费）；输出图 ≤261 万像素 0.3 元/张，>261 万像素 0.6 元/张",
}
PRICING_SEEDANCE_MINI = {
    "kind": "token_output",
    "outputPer1M": "23",
    "outputPer1MWithVideoInput": "14",
    "currency": "CNY",
    "note": "不含视频输入 23 元/百万 tokens；含视频输入 14 元/百万 tokens",
}
PRICING_SEEDANCE_FAST = {
    "kind": "token_output",
    "outputPer1M": "37",
    "outputPer1MWithVideoInput": "22",
    "currency": "CNY",
    "note": "不含视频输入 37 元/百万 tokens；含视频输入 22 元/百万 tokens",
}
PRICING_SEEDANCE_25 = {
    "kind": "token_output",
    "outputPer1M": "70",
    "outputPer1MWithVideoInput": "42",
    "byResolution": {
        "480p": {"outputPer1M": "70", "outputPer1MWithVideoInput": "42"},
        "720p": {"outputPer1M": "70", "outputPer1MWithVideoInput": "42"},
        "1080p": {"outputPer1M": "77", "outputPer1MWithVideoInput": "46"},
    },
    "currency": "CNY",
    "note": "480P/720P：不含视频输入 70、含 42；1080P：不含 77、含 46（元/百万 tokens）",
}


def _config(
    *,
    capability: ProviderCapability,
    product_label: str,
    endpoint_id: str,
    credential_ref: str,
    price_unit: str,
    price_amount: str = "0",
    concurrency_limit: int = 1,
    is_default: bool = False,
    spec: dict[str, Any] | None = None,
    pricing: dict[str, Any] | None = None,
    note: str = "",
) -> ModelConfig:
    merged_spec: dict[str, Any] = dict(spec or {})
    if pricing:
        merged_spec["pricing"] = pricing
    return ModelConfig(
        provider="ark",
        capability=capability,
        product_label=product_label,
        endpoint_id=endpoint_id,
        # 实际模型标识要等真实响应回填，先留空
        actual_model="",
        enabled=True,
        is_default=is_default,
        verified_at=None,
        spec_json=json.dumps(merged_spec, ensure_ascii=False),
        price_amount=Decimal(price_amount),
        price_currency="CNY",
        price_unit=price_unit,
        concurrency_limit=concurrency_limit,
        credential_ref=credential_ref,
        note=note,
    )


DEFAULT_MODEL_CONFIGS: list[ModelConfig] = [
    _config(
        capability=ProviderCapability.text,
        product_label="doubao",
        endpoint_id=settings.ARK_DOUBAO_MODEL,
        credential_ref="ARK_DOUBAO_API_KEY",
        # 展示口径取输出价（更贵的那一档），完整口径在 spec.pricing 里
        price_unit="per_1m_output_token",
        price_amount="15",
        is_default=True,
        spec={
            "supportsJsonMode": True,
            "supportsImageInput": "unverified",
            "smokeTested": True,
            "smokeTestNote": "2026-09-20 HTTP 200；实际模型 doubao-seed-2-1-turbo-260628",
        },
        pricing=PRICING_DOUBAO,
        note="文本与提示词；结构化任务必须请求 JSON 模式并做 schema 校验。输入/输出单价不同",
    ),
    _config(
        capability=ProviderCapability.image,
        product_label="seedream-lite",
        endpoint_id=settings.ARK_SEEDREAM_LITE_MODEL,
        credential_ref="ARK_SEEDREAM_LITE_API_KEY",
        price_unit="per_image",
        price_amount="0.22",
        is_default=True,
        spec={
            "minPixels": 3686400,
            "maxEdge": 4096,
            "supportsTextToImage": True,
            "supportsImageToImage": True,
            "maxReferenceImages": 1,
            "smokeTested": True,
            "smokeTestNote": "2026-09-20 单源图生图 1920x1920 HTTP 200",
        },
        pricing=PRICING_SEEDREAM_LITE,
        note="默认档位；1024x1024 会被接口拒绝，提交前必须校验最小像素",
    ),
    _config(
        capability=ProviderCapability.image,
        product_label="seedream-pro",
        endpoint_id=settings.ARK_SEEDREAM_PRO_MODEL,
        credential_ref="ARK_SEEDREAM_PRO_API_KEY",
        price_unit="per_image",
        price_amount="0.3",
        spec={
            "minPixels": 3686400,
            "supportsTextToImage": "unverified",
            "supportsImageToImage": True,
            "maxReferenceImages": 1,
            "smokeTested": True,
            "smokeTestNote": "2026-09-20 单源图生图 1920x1920 HTTP 200",
        },
        pricing=PRICING_SEEDREAM_PRO,
        note=(
            "pro 档位按输出图像素分档。注意：官方要求最小面积 3,686,400 像素，"
            "已经高于 261 万像素档，因此本版常用尺寸都会落在 0.6 元/张那一档"
        ),
    ),
    _config(
        capability=ProviderCapability.video,
        product_label="seedance-mini",
        endpoint_id=settings.ARK_SEEDANCE_MINI_MODEL,
        credential_ref="ARK_SEEDANCE_MINI_API_KEY",
        price_unit="per_1m_token",
        price_amount="23",
        is_default=True,
        concurrency_limit=3,
        spec={
            "resolutions": ["480p", "720p"],
            "durationRangeSeconds": [4, 15],
            "frameRate": 24,
            "maxReferenceImages": 9,
            "verified": True,
        },
        pricing=PRICING_SEEDANCE_MINI,
        note="规格与计价取自官方档位表；本版只发参考图，因此按「不含视频输入」计价",
    ),
    _config(
        capability=ProviderCapability.video,
        product_label="seedance-fast",
        endpoint_id=settings.ARK_SEEDANCE_FAST_MODEL,
        credential_ref="ARK_SEEDANCE_FAST_API_KEY",
        price_unit="per_1m_token",
        price_amount="37",
        concurrency_limit=3,
        spec={
            "resolutions": ["480p", "720p"],
            "durationRangeSeconds": [4, 15],
            "frameRate": 24,
            "maxReferenceImages": 9,
            "verified": False,
        },
        pricing=PRICING_SEEDANCE_FAST,
        note="规格与计价取自官方档位表，尚未发起真实任务",
    ),
    _config(
        capability=ProviderCapability.video,
        product_label="seedance-2.5",
        endpoint_id=settings.ARK_SEEDANCE_25_MODEL,
        credential_ref="ARK_SEEDANCE_25_API_KEY",
        price_unit="per_1m_token",
        price_amount="70",
        concurrency_limit=3,
        spec={
            "resolutions": ["480p", "720p", "1080p"],
            "durationRangeSeconds": [4, 30],
            "frameRate": 24,
            "maxReferenceImages": 9,
            "verified": False,
        },
        pricing=PRICING_SEEDANCE_25,
        note="规格与计价取自官方档位表，尚未发起真实任务；PRD 里显示为 2.5",
    ),
]


def ensure_model_configs(db: Session) -> int:
    """按 (provider, capability, product_label) 补齐缺失档位；不覆盖已有配置。"""
    created = 0
    for config in DEFAULT_MODEL_CONFIGS:
        existing = db.exec(
            select(ModelConfig).where(
                ModelConfig.provider == config.provider,
                ModelConfig.capability == config.capability,
                ModelConfig.product_label == config.product_label,
            )
        ).first()
        if existing is not None:
            continue
        db.add(config)
        created += 1
    if created:
        db.commit()
    return created


def sync_model_config_endpoints(db: Session) -> int:
    """把接入点、凭证引用与计价口径同步成当前部署配置。

    只改这三个**部署级**字段——换环境变量名、换接入点、官方调价都不该要求手改数据库。
    启停、规格、核验时间属于使用侧配置，一律不动。
    """
    changed = 0
    for config in DEFAULT_MODEL_CONFIGS:
        existing = db.exec(
            select(ModelConfig).where(
                ModelConfig.provider == config.provider,
                ModelConfig.capability == config.capability,
                ModelConfig.product_label == config.product_label,
            )
        ).first()
        if existing is None:
            continue

        default_spec = json.loads(config.spec_json or "{}")
        current_spec = json.loads(existing.spec_json or "{}")
        pricing_changed = current_spec.get("pricing") != default_spec.get("pricing")

        if (
            existing.endpoint_id != config.endpoint_id
            or existing.credential_ref != config.credential_ref
            or pricing_changed
        ):
            existing.endpoint_id = config.endpoint_id
            existing.credential_ref = config.credential_ref
            if pricing_changed:
                # 只覆盖计价字段，保留使用者可能补过的其它 spec 项
                current_spec["pricing"] = default_spec.get("pricing")
                existing.spec_json = json.dumps(current_spec, ensure_ascii=False)
                existing.price_amount = config.price_amount
                existing.price_unit = config.price_unit
            db.add(existing)
            changed += 1
    if changed:
        db.commit()
    return changed
