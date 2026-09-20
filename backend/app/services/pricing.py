"""计费：按各模型真实的计价口径折算金额。

单价来源是用户提供的**当前官方报价**（2026-09-20），不是历史资料：

| 模型 | 计价口径 |
|---|---|
| 豆包 | 推理输入 3 元/百万 tokens；推理输出 15 元/百万 tokens |
| seedance 2.0 mini | 含视频输入 14、不含视频输入 23 元/百万 tokens |
| seedance 2.0 fast | 含视频输入 22、不含视频输入 37 元/百万 tokens |
| seedance 2.5 480P/720P | 含视频输入 42、不含视频输入 70 元/百万 tokens |
| seedance 2.5 1080P | 含视频输入 46、不含视频输入 77 元/百万 tokens |
| seedream 5.0 pro | 输入图 0.02 元/张（首张免费）；输出图 ≤261 万像素 0.3 元/张，>261 万像素 0.6 元/张 |
| seedream 5.0 lite | 文生图 / 图生图 均 0.22 元/张 |

计价规则存在 `model_configs.spec_json.pricing`，**改动单价不需要改代码**。

铁律：单价缺失或口径不认识时记 `unknown` 与 `None`，**绝不记 0**——0 会被读成「免费」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models.enums import BillingStatus
from app.models.model_config import ModelConfig

MILLION = Decimal(1_000_000)

#: 视频输出 token 速率（个/秒），来自 AI漫剧制作工作流执行手册 §3.2。
#: 仅用于**提交前预估**；实际金额一律用供应商回执里的真实用量折算。
VIDEO_TOKENS_PER_SECOND = {
    "480p": Decimal(10845),
    "720p": Decimal(21690),
    "1080p": Decimal(48667),
}


@dataclass
class PriceResult:
    amount: Decimal | None = None
    status: BillingStatus = BillingStatus.unknown
    note: str = ""
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    estimated: Decimal | None = None


def pricing_spec(config: ModelConfig | None) -> dict:
    if config is None:
        return {}
    try:
        spec = json.loads(config.spec_json or "{}")
    except json.JSONDecodeError:
        return {}
    pricing = spec.get("pricing")
    return pricing if isinstance(pricing, dict) else {}


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - 单价写错就当没有
        return None


def calculate(
    config: ModelConfig | None,
    *,
    usage: dict,
    resolution: str = "",
    includes_video_input: bool = False,
    output_pixels: int | None = None,
    is_mock: bool = False,
) -> PriceResult:
    """按真实用量折算金额。缺任何一项必要信息就记 unknown，不猜。"""
    if is_mock:
        return PriceResult(
            amount=Decimal("0"),
            estimated=Decimal("0"),
            status=BillingStatus.mock,
            note="mock 不计入实际支出",
        )

    spec = pricing_spec(config)
    if not spec:
        return PriceResult(
            status=BillingStatus.unknown,
            note="模型配置未登记计价口径，金额待核对",
        )

    kind = str(spec.get("kind") or "")
    if kind == "token_pair":
        return _token_pair(spec, usage)
    if kind == "token_output":
        return _token_output(spec, usage, resolution=resolution, includes_video_input=includes_video_input)
    if kind == "per_image":
        return _per_image(spec, usage)
    if kind == "seedream_pro":
        return _seedream_pro(spec, usage, output_pixels=output_pixels)

    return PriceResult(
        status=BillingStatus.unknown,
        note=f"未实现的计价口径 {kind!r}，金额待核对",
    )


def _token_pair(spec: dict, usage: dict) -> PriceResult:
    """豆包：输入与输出单价不同，必须分开乘。"""
    in_price = _dec(spec.get("inputPer1M"))
    out_price = _dec(spec.get("outputPer1M"))
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if in_price is None or out_price is None:
        return PriceResult(status=BillingStatus.unknown, note="输入/输出单价缺失，金额待核对")
    if input_tokens is None and output_tokens is None:
        return PriceResult(status=BillingStatus.unknown, note="未取到 token 用量，金额待核对")

    input_cost = (Decimal(int(input_tokens or 0)) * in_price) / MILLION
    output_cost = (Decimal(int(output_tokens or 0)) * out_price) / MILLION
    return PriceResult(
        amount=input_cost + output_cost,
        estimated=input_cost + output_cost,
        status=BillingStatus.estimated,
        note="按当前官方单价与本次回执用量折算，未经账单确认",
        breakdown=[
            {"item": "input_tokens", "units": int(input_tokens or 0), "unitPrice": str(in_price), "amount": str(input_cost)},
            {"item": "output_tokens", "units": int(output_tokens or 0), "unitPrice": str(out_price), "amount": str(output_cost)},
        ],
    )


def _token_output(
    spec: dict, usage: dict, *, resolution: str, includes_video_input: bool
) -> PriceResult:
    """视频：只按输出 token 计价；2.5 档还要按分辨率分档。"""
    by_resolution = spec.get("byResolution") or {}
    key = (resolution or "").lower()
    tier = by_resolution.get(key) or spec

    field = "outputPer1MWithVideoInput" if includes_video_input else "outputPer1M"
    price = _dec(tier.get(field)) or _dec(tier.get("outputPer1M"))
    if price is None:
        return PriceResult(status=BillingStatus.unknown, note="该分辨率/口径没有登记单价，金额待核对")

    tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    if tokens is None:
        return PriceResult(status=BillingStatus.unknown, note="未取到输出 token 用量，金额待核对")

    amount = (Decimal(int(tokens)) * price) / MILLION
    return PriceResult(
        amount=amount,
        estimated=amount,
        status=BillingStatus.estimated,
        note=(
            "按当前官方单价（不含视频输入）与本次回执 token 折算，未经账单确认"
            if not includes_video_input
            else "按当前官方单价（含视频输入）与本次回执 token 折算，未经账单确认"
        ),
        breakdown=[
            {
                "item": "output_tokens",
                "units": int(tokens),
                "unitPrice": str(price),
                "resolution": key or "默认",
                "amount": str(amount),
            }
        ],
    )


def _per_image(spec: dict, usage: dict) -> PriceResult:
    """seedream lite：文生图与图生图同价，按张计。"""
    price = _dec(spec.get("perImage"))
    if price is None:
        return PriceResult(status=BillingStatus.unknown, note="单张价格缺失，金额待核对")
    count = int(usage.get("generated_images") or 1)
    amount = price * Decimal(count)
    return PriceResult(
        amount=amount,
        estimated=amount,
        status=BillingStatus.estimated,
        note="按当前官方单价与产物张数折算，未经账单确认",
        breakdown=[{"item": "generated_images", "units": count, "unitPrice": str(price), "amount": str(amount)}],
    )


def _seedream_pro(spec: dict, usage: dict, *, output_pixels: int | None) -> PriceResult:
    """seedream pro：输入图按张（首张免费）+ 输出图按像素档。"""
    input_price = _dec(spec.get("inputImagePerImage")) or Decimal("0")
    base_price = _dec(spec.get("outputImageBase"))
    large_price = _dec(spec.get("outputImageLarge"))
    threshold = int(spec.get("largePixelThreshold") or 0)
    if base_price is None or large_price is None:
        return PriceResult(status=BillingStatus.unknown, note="输出图单价缺失，金额待核对")

    input_images = int(usage.get("input_images") or 0)
    generated = int(usage.get("generated_images") or 1)

    billable_inputs = input_images
    if spec.get("firstInputImageFree") and input_images > 0:
        billable_inputs = input_images - 1
    input_cost = input_price * Decimal(billable_inputs)

    # 输出图单价由**产物的实际像素**决定，不看请求参数
    is_large = bool(threshold and output_pixels and output_pixels > threshold)
    unit = large_price if is_large else base_price
    output_cost = unit * Decimal(generated)

    return PriceResult(
        amount=input_cost + output_cost,
        estimated=input_cost + output_cost,
        status=BillingStatus.estimated,
        note=(
            f"输入图 {input_images} 张（首张免费，计费 {billable_inputs} 张）+ 输出图 {generated} 张"
            f"（{'＞' if is_large else '≤'}{threshold // 10000} 万像素档），未经账单确认"
            if threshold
            else "未经账单确认"
        ),
        breakdown=[
            {"item": "input_images", "units": billable_inputs, "unitPrice": str(input_price), "amount": str(input_cost)},
            {
                "item": "output_images",
                "units": generated,
                "unitPrice": str(unit),
                "pixels": output_pixels,
                "amount": str(output_cost),
            },
        ],
    )


# --- 提交前预估 ---------------------------------------------------------


def estimate(
    config: ModelConfig | None,
    *,
    operation: str,
    count: int = 1,
    duration_seconds: int = 5,
    resolution: str = "720p",
    image_pixels: int | None = None,
    source_images: int = 0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    is_mock: bool = False,
) -> dict:
    """生成前预估。能给准的给准，给不准的写清「按什么假设估的」。"""
    if is_mock:
        return {
            "currency": "CNY",
            "estimatedAmount": "0",
            "isMock": True,
            "modelConfigId": None,
            "unit": "",
            "notes": ["当前为 mock 模式，不发外部请求，费用记为模拟且不计入实际支出"],
        }

    spec = pricing_spec(config)
    notes: list[str] = []
    if not spec:
        notes.append("模型配置未登记计价口径，无法预估")
        return {
            "currency": config.price_currency if config else "CNY",
            "estimatedAmount": "0",
            "isMock": False,
            "modelConfigId": config.id if config else None,
            "unit": "",
            "notes": notes,
        }

    kind = spec.get("kind")
    amount: Decimal | None = None
    unit = ""

    if kind == "token_pair":
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        result = _token_pair(spec, {k: v for k, v in usage.items() if v is not None})
        amount = result.amount
        unit = "元/百万 tokens"
        if input_tokens is None:
            notes.append("输入 token 未提供，按 0 计；实际金额以回执用量为准")
        if output_tokens is None:
            notes.append("输出 token 未提供，按 0 计；结构化任务的输出通常在一千以上")
    elif kind == "token_output":
        per_second = VIDEO_TOKENS_PER_SECOND.get(resolution.lower(), VIDEO_TOKENS_PER_SECOND["720p"])
        tokens = int(per_second * Decimal(max(duration_seconds, 1)))
        result = _token_output(spec, {"completion_tokens": tokens}, resolution=resolution, includes_video_input=False)
        amount = result.amount
        unit = "元/百万 tokens"
        notes.append(
            f"按手册的 {per_second} tokens/秒 估算输出用量（{tokens} tokens），实际以回执为准"
        )
    elif kind == "per_image":
        result = _per_image(spec, {"generated_images": count})
        amount = result.amount
        unit = "元/张"
    elif kind == "seedream_pro":
        usage = {"input_images": source_images, "generated_images": count}
        result = _seedream_pro(spec, usage, output_pixels=image_pixels)
        amount = result.amount
        unit = "元/张"
        if image_pixels:
            threshold = int(spec.get("largePixelThreshold") or 0)
            notes.append(
                f"按 {image_pixels} 像素计（{'＞' if image_pixels > threshold else '≤'}{threshold // 10000} 万像素档）"
            )
    else:
        notes.append(f"未实现的计价口径 {kind!r}")

    return {
        "currency": (config.price_currency if config else "CNY"),
        "estimatedAmount": str(amount) if amount is not None else "0",
        "isMock": False,
        "modelConfigId": config.id if config else None,
        "unit": unit,
        "notes": notes,
    }


__all__ = ["PriceResult", "calculate", "estimate", "pricing_spec"]
