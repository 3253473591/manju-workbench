"""费用服务：预估、明细与项目合计。

规则（API对接文档 §6.2）：
- 按 attempt 去重，一个 attempt 被多个节点引用不重复记账。
- 用量或价格缺失记为未知，不默认为 0。
- mock 明确标识，不计入实际支出。
- user 角色不返回任何计费字段（路由层用 require_roles 拦住）。
"""

from __future__ import annotations

from decimal import Decimal

from sqlmodel import Session, select

from app.models.enums import BillingStatus, ProviderCapability
from app.models.generation import GenerationAttempt, GenerationSubmission
from app.models.model_config import ModelConfig, UsageRecord
from app.providers import registry
from app.services import pricing

ZERO = Decimal("0")


def _money(value: Decimal | None) -> str:
    return str(value if value is not None else ZERO)


def project_billing_summary(db: Session, project_id: str) -> dict:
    from app.models.project import Episode

    episode_ids = [
        e.id
        for e in db.exec(select(Episode).where(Episode.project_id == project_id)).all()
    ]
    currency = "CNY"
    if not episode_ids:
        return {
            "currency": currency,
            "attemptCount": 0,
            "actualTotal": "0",
            "estimatedTotal": "0",
            "mockTotal": "0",
            "byOperation": [],
            "unknownUsageCount": 0,
        }

    submissions = list(
        db.exec(
            select(GenerationSubmission).where(
                GenerationSubmission.episode_id.in_(episode_ids)  # type: ignore[union-attr]
            )
        ).all()
    )
    submission_ids = [s.id for s in submissions]
    if not submission_ids:
        return {
            "currency": currency,
            "attemptCount": 0,
            "actualTotal": "0",
            "estimatedTotal": "0",
            "mockTotal": "0",
            "byOperation": [],
            "unknownUsageCount": 0,
        }

    attempts = list(
        db.exec(
            select(GenerationAttempt).where(
                GenerationAttempt.submission_id.in_(submission_ids)  # type: ignore[union-attr]
            )
        ).all()
    )

    actual_total = ZERO
    estimated_total = ZERO
    mock_total = ZERO
    unknown = 0
    by_operation: dict[str, dict] = {}

    for attempt in attempts:
        usage = db.exec(
            select(UsageRecord).where(UsageRecord.attempt_id == attempt.id).limit(1)
        ).first()
        bucket = by_operation.setdefault(
            attempt.operation.value, {"operation": attempt.operation.value, "attemptCount": 0, "amount": ZERO}
        )
        # attempt 级去重：每个 attempt 只计一次
        bucket["attemptCount"] += 1

        if usage is None:
            unknown += 1
            continue
        if usage.is_mock:
            mock_total += usage.actual_amount or ZERO
            continue
        if usage.actual_amount is None:
            unknown += 1
            continue
        actual_total += usage.actual_amount
        estimated_total += usage.estimated_amount or ZERO
        bucket["amount"] += usage.actual_amount

    return {
        "currency": currency,
        "attemptCount": len(attempts),
        "actualTotal": _money(actual_total),
        "estimatedTotal": _money(estimated_total),
        "mockTotal": _money(mock_total),
        "byOperation": [
            {
                "operation": entry["operation"],
                "attemptCount": entry["attemptCount"],
                "amount": _money(entry["amount"]),
            }
            for entry in by_operation.values()
        ],
        "unknownUsageCount": unknown,
    }


def project_billing_records(db: Session, project_id: str, *, page: int, page_size: int) -> dict:
    from app.models.project import Episode

    episode_ids = [
        e.id for e in db.exec(select(Episode).where(Episode.project_id == project_id)).all()
    ]
    if not episode_ids:
        return {"items": [], "total": 0, "page": page, "pageSize": page_size}

    submissions = list(
        db.exec(
            select(GenerationSubmission).where(
                GenerationSubmission.episode_id.in_(episode_ids)  # type: ignore[union-attr]
            )
        ).all()
    )
    submission_map = {s.id: s for s in submissions}
    if not submission_map:
        return {"items": [], "total": 0, "page": page, "pageSize": page_size}

    attempts = list(
        db.exec(
            select(GenerationAttempt)
            .where(GenerationAttempt.submission_id.in_(list(submission_map)))  # type: ignore[union-attr]
            .order_by(GenerationAttempt.created_at.desc())
        ).all()
    )
    total = len(attempts)
    window = attempts[(page - 1) * page_size : page * page_size]

    items = []
    for attempt in window:
        usage = db.exec(
            select(UsageRecord).where(UsageRecord.attempt_id == attempt.id).limit(1)
        ).first()
        currency = "CNY"
        if attempt.model_config_id:
            config = db.get(ModelConfig, attempt.model_config_id)
            if config is not None:
                currency = config.price_currency
        items.append(
            {
                "attemptId": attempt.id,
                "submissionId": attempt.submission_id,
                "operation": attempt.operation.value,
                "modelConfigId": attempt.model_config_id,
                "inputUnits": usage.input_units if usage else None,
                "outputUnits": usage.output_units if usage else None,
                "amount": (
                    str(usage.actual_amount)
                    if usage and usage.actual_amount is not None
                    else None
                ),
                "currency": currency,
                "billingStatus": (usage.billing_status.value if usage else BillingStatus.unknown.value),
                "isMock": bool(usage.is_mock) if usage else False,
                "createdAt": attempt.created_at.isoformat() + "Z",
            }
        )
    return {"items": items, "total": total, "page": page, "pageSize": page_size}


def estimate_generation(
    db: Session,
    *,
    operation: str,
    count: int = 1,
    product_label: str | None = None,
    model_config_id: str | None = None,
    duration_seconds: int = 5,
    resolution: str = "720p",
    image_pixels: int | None = None,
    source_images: int = 0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict:
    """生成前预估。计价口径统一交给 `services.pricing`，与事后折算同一套规则。"""
    from app.core.config import settings

    capability = _capability_for(operation)
    if settings.is_mock:
        return pricing.estimate(None, operation=operation, is_mock=True)

    config = registry.find_model_config(
        db,
        capability,
        product_label=registry.product_label_for(capability, product_label),
        config_id=model_config_id,
    )
    notes: list[str] = []
    if config.verified_at is None:
        notes.append("该档位尚未通过真实调用核验，预估仅供比较")

    result = pricing.estimate(
        config,
        operation=operation,
        count=count,
        duration_seconds=duration_seconds,
        resolution=resolution,
        image_pixels=image_pixels,
        source_images=source_images,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    result["notes"] = notes + list(result.get("notes") or [])
    return result


def _capability_for(operation: str) -> ProviderCapability:
    if operation.startswith("image_") or operation == "plan_characters":
        return ProviderCapability.image
    if operation.startswith("video_"):
        return ProviderCapability.video
    return ProviderCapability.text
