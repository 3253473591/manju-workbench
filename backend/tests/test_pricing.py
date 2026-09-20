"""计费折算：用用户提供的官方报价逐项核对。

单价口径（2026-09-20 用户提供）：
- 豆包：输入 3、输出 15（元/百万 tokens）
- seedance mini：不含视频输入 23、含 14
- seedance fast：不含视频输入 37、含 22
- seedance 2.5 480P/720P：不含 70、含 42；1080P：不含 77、含 46
- seedream pro：输入图 0.02/张（首张免费）；输出图 ≤261 万像素 0.3/张，>261 万像素 0.6/张
- seedream lite：0.22/张
"""

from __future__ import annotations

import json
from decimal import Decimal

from app.db.seed import (
    PRICING_DOUBAO,
    PRICING_SEEDANCE_25,
    PRICING_SEEDANCE_FAST,
    PRICING_SEEDANCE_MINI,
    PRICING_SEEDREAM_LITE,
    PRICING_SEEDREAM_PRO,
)
from app.models.enums import BillingStatus, ProviderCapability
from app.models.model_config import ModelConfig
from app.services import pricing


def config_with(pricing_spec: dict, capability=ProviderCapability.text) -> ModelConfig:
    return ModelConfig(
        provider="ark",
        capability=capability,
        product_label="test",
        spec_json=json.dumps({"pricing": pricing_spec}),
    )


def amount(result: pricing.PriceResult) -> Decimal:
    assert result.amount is not None, result.note
    return result.amount


# --- 豆包 ---------------------------------------------------------------


def test_doubao_input_and_output_are_priced_separately() -> None:
    """输入 3、输出 15——两边单价不同，不能拿一个价乘两个数。"""
    config = config_with(PRICING_DOUBAO)
    result = pricing.calculate(
        config, usage={"input_tokens": 1000, "output_tokens": 2000}
    )
    # 1000*3/1e6 + 2000*15/1e6 = 0.003 + 0.030 = 0.033
    assert amount(result) == Decimal("0.033")
    assert result.status == BillingStatus.estimated


def test_doubao_matches_real_run_figures() -> None:
    """用 real 全链路实测的用量核对：剧本步骤 输入 1016 / 输出 7352。"""
    config = config_with(PRICING_DOUBAO)
    result = pricing.calculate(
        config, usage={"input_tokens": 1016, "output_tokens": 7352}
    )
    # 1016*3/1e6 + 7352*15/1e6 = 0.003048 + 0.11028
    assert amount(result) == Decimal("0.113328")


# --- Seedance -----------------------------------------------------------


def test_seedance_mini_without_video_input() -> None:
    """本版只发参考图，按「不含视频输入」计价。"""
    config = config_with(PRICING_SEEDANCE_MINI, ProviderCapability.video)
    result = pricing.calculate(
        config,
        usage={"completion_tokens": 108900, "duration_seconds": 5},
        resolution="720p",
        includes_video_input=False,
    )
    # 108900 * 23 / 1e6 = 2.5047
    assert amount(result) == Decimal("2.5047")


def test_seedance_mini_with_video_input_is_cheaper() -> None:
    config = config_with(PRICING_SEEDANCE_MINI, ProviderCapability.video)
    result = pricing.calculate(
        config, usage={"completion_tokens": 108900}, includes_video_input=True
    )
    assert amount(result) == Decimal("1.5246")


def test_seedance_fast_pricing() -> None:
    config = config_with(PRICING_SEEDANCE_FAST, ProviderCapability.video)
    result = pricing.calculate(config, usage={"completion_tokens": 108900})
    assert amount(result) == Decimal("4.0293")  # 108900 * 37 / 1e6


def test_seedance_25_differs_by_resolution() -> None:
    config = config_with(PRICING_SEEDANCE_25, ProviderCapability.video)
    at_720 = pricing.calculate(
        config, usage={"completion_tokens": 100000}, resolution="720p"
    )
    at_1080 = pricing.calculate(
        config, usage={"completion_tokens": 100000}, resolution="1080p"
    )
    assert amount(at_720) == Decimal("7")  # 100000 * 70 / 1e6
    assert amount(at_1080) == Decimal("7.7")  # 100000 * 77 / 1e6


def test_seedance_25_480p_uses_720p_rate() -> None:
    config = config_with(PRICING_SEEDANCE_25, ProviderCapability.video)
    result = pricing.calculate(
        config, usage={"completion_tokens": 100000}, resolution="480p"
    )
    assert amount(result) == Decimal("7")


# --- Seedream -----------------------------------------------------------


def test_seedream_lite_is_flat_per_image() -> None:
    config = config_with(PRICING_SEEDREAM_LITE, ProviderCapability.image)
    result = pricing.calculate(config, usage={"generated_images": 4})
    assert amount(result) == Decimal("0.88")


def test_seedream_pro_large_output_and_free_first_input() -> None:
    """1920×1920 = 3,686,400 像素 > 261 万 → 0.6 元/张；输入图首张免费。"""
    config = config_with(PRICING_SEEDREAM_PRO, ProviderCapability.image)
    result = pricing.calculate(
        config,
        usage={"input_images": 1, "generated_images": 1},
        output_pixels=1920 * 1920,
    )
    assert amount(result) == Decimal("0.6")


def test_seedream_pro_charges_second_input_image() -> None:
    config = config_with(PRICING_SEEDREAM_PRO, ProviderCapability.image)
    result = pricing.calculate(
        config,
        usage={"input_images": 2, "generated_images": 1},
        output_pixels=1920 * 1920,
    )
    # 输出 0.6 + 输入 (2-1)*0.02
    assert amount(result) == Decimal("0.62")


def test_seedream_pro_base_tier_for_small_output() -> None:
    config = config_with(PRICING_SEEDREAM_PRO, ProviderCapability.image)
    result = pricing.calculate(
        config,
        usage={"input_images": 0, "generated_images": 1},
        output_pixels=1024 * 1024,  # 1,048,576 ≤ 261 万
    )
    assert amount(result) == Decimal("0.3")


def test_seedream_pro_note_explains_the_tier() -> None:
    config = config_with(PRICING_SEEDREAM_PRO, ProviderCapability.image)
    result = pricing.calculate(
        config,
        usage={"input_images": 1, "generated_images": 1},
        output_pixels=1920 * 1920,
    )
    assert "首张免费" in result.note
    assert "＞" in result.note or ">" in result.note


# --- 兜底 ---------------------------------------------------------------


def test_missing_pricing_is_unknown_not_zero() -> None:
    """没有计价口径时必须记 unknown，**不能记 0**——0 会被读成免费。"""
    config = ModelConfig(
        provider="ark",
        capability=ProviderCapability.text,
        product_label="no-pricing",
        spec_json="{}",
    )
    result = pricing.calculate(config, usage={"input_tokens": 100, "output_tokens": 100})
    assert result.amount is None
    assert result.status == BillingStatus.unknown
    assert result.note


def test_missing_usage_is_unknown_not_zero() -> None:
    config = config_with(PRICING_DOUBAO)
    result = pricing.calculate(config, usage={})
    assert result.amount is None
    assert result.status == BillingStatus.unknown


def test_mock_is_marked_and_zero() -> None:
    config = config_with(PRICING_DOUBAO)
    result = pricing.calculate(config, usage={"input_tokens": 999}, is_mock=True)
    assert result.status == BillingStatus.mock
    assert result.amount == Decimal("0")
    assert "mock" in result.note


# --- 提交前预估 ---------------------------------------------------------


def test_estimate_video_uses_manual_token_rate() -> None:
    config = config_with(PRICING_SEEDANCE_MINI, ProviderCapability.video)
    result = pricing.estimate(
        config, operation="video_draw", duration_seconds=5, resolution="720p"
    )
    # 720p ≈ 21690 tokens/秒 × 5s = 108450 → × 23/1e6
    assert result["estimatedAmount"] == "2.49435"
    assert any("tokens/秒" in note for note in result["notes"])


def test_estimate_image_is_exact() -> None:
    config = config_with(PRICING_SEEDREAM_LITE, ProviderCapability.image)
    result = pricing.estimate(config, operation="image_draw", count=3)
    assert result["estimatedAmount"] == "0.66"
    assert result["unit"] == "元/张"


def test_estimate_seedream_pro_note_mentions_pixel_tier() -> None:
    config = config_with(PRICING_SEEDREAM_PRO, ProviderCapability.image)
    result = pricing.estimate(
        config,
        operation="image_draw",
        count=1,
        image_pixels=1920 * 1920,
        source_images=1,
    )
    assert Decimal(result["estimatedAmount"]) == Decimal("0.6")
    assert any("像素" in note for note in result["notes"])
