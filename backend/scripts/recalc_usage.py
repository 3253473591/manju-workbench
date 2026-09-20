"""按当前登记的计价口径重算历史 usage_records。

用途：官方调价或修正计价口径后，**不需要重跑生成**也能把账算对。
只更新金额与计费状态，不动用量原始回执。

用法：
    run.cmd scripts\\recalc_usage.py            # 全量
    run.cmd scripts\\recalc_usage.py --dry-run  # 只看差异
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlmodel import select  # noqa: E402

from app.db.engine import session_scope  # noqa: E402
from app.models.enums import Operation  # noqa: E402
from app.models.generation import DeliveryRecord, GenerationAttempt  # noqa: E402
from app.models.media import MediaItem  # noqa: E402
from app.models.model_config import ModelConfig, UsageRecord  # noqa: E402
from app.services import pricing  # noqa: E402

IMAGE_OPERATIONS = {Operation.image_draw, Operation.image_derive}


def _split_unit_json(raw: str) -> tuple[dict, dict]:
    """兼容两种历史格式：早期只存 usage，后期存 {usage, context, breakdown}。"""
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}, {}
    if "usage" in payload and isinstance(payload["usage"], dict):
        return payload["usage"], payload.get("context") or {}
    return payload, {}


def _output_pixels(db, attempt) -> int | None:  # type: ignore[no-untyped-def]
    """图片类 attempt 的产物像素：走 delivery_records → file_key → MediaItem。"""
    delivery = db.exec(
        select(DeliveryRecord).where(DeliveryRecord.attempt_id == attempt.id).limit(1)
    ).first()
    if delivery is None or not delivery.file_key:
        return None
    item = db.exec(
        select(MediaItem).where(MediaItem.file_key == delivery.file_key)
    ).first()
    if item is None or not item.width or not item.height:
        return None
    return item.width * item.height


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with session_scope() as db:
        records = list(db.exec(select(UsageRecord)).all())
        configs = {c.id: c for c in db.exec(select(ModelConfig)).all()}
        attempts = {a.id: a for a in db.exec(select(GenerationAttempt)).all()}

        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        unknown = 0
        changed = 0

        for record in records:
            attempt = attempts.get(record.attempt_id)
            if attempt is None:
                continue
            usage, context = _split_unit_json(record.unit_json)
            config = configs.get(attempt.model_config_id) if attempt.model_config_id else None

            if not context.get("output_pixels") and attempt.operation in IMAGE_OPERATIONS:
                pixels = _output_pixels(db, attempt)
                if pixels:
                    context["output_pixels"] = pixels
            if not context.get("resolution"):
                context["resolution"] = usage.get("resolution") or "720p"

            result = pricing.calculate(
                config,
                usage=usage,
                resolution=str(context.get("resolution") or ""),
                includes_video_input=bool(context.get("includes_video_input")),
                output_pixels=context.get("output_pixels"),
                is_mock=attempt.is_mock,
            )

            old = record.actual_amount
            new = result.amount
            if old != new or record.billing_status != result.status:
                changed += 1
                print(
                    f"{attempt.operation.value:22} {str(old):>10} -> {str(new):>10}  "
                    f"{result.status.value}"
                )
            record.actual_amount = new
            record.estimated_amount = result.estimated
            record.billing_status = result.status
            record.note = result.note
            record.unit_json = json.dumps(
                {"usage": usage, "context": context, "breakdown": result.breakdown},
                ensure_ascii=False,
                default=str,
            )
            db.add(record)

            if result.status.value == "unknown":
                unknown += 1
            else:
                totals[result.status.value] += new or Decimal("0")

        print()
        print(f"记录数 {len(records)}；变更 {changed}；仍待核对 {unknown}")
        for status, total in sorted(totals.items()):
            print(f"  {status:10} {total} CNY")
        if args.dry_run:
            print("\n--dry-run：以上变更未提交")
            db.rollback()
        else:
            print("\n已提交")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
