"""供应商任务恢复（reconciler）。

交付语义（PRD §7.3 / 审查报告 P1-4）：一次供应商视频任务提交后，进程崩溃、
轮询超时或下载失败都**不能**变成「重新提交一次」。凡是已经拿到
`provider_task_id` 的 attempt，只能按任务 ID 复查既有任务：

- 供应商已成功 → 走与抽卡完全相同的交付路径落盘、出结果节点、核验；
- 供应商明确失败/取消/过期 → attempt 与 submission 一起收敛到 failed；
- 状态仍然未知 → 保留 unknown，按间隔重试；达到次数上限后收敛为
  `PROVIDER_TASK_UNRESOLVED`，让节点锁释放，但**不**自动重新收费。

没有 `provider_task_id` 的 attempt 不归这里管：它们由
`generation_service.recover_stale_submissions` 按租约回收。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlmodel import Session, select

from app.core.config import settings
from app.core.utils import utcnow
from app.models.common import touch
from app.models.enums import LogicalStatus, MediaKind, Operation, PhysicalStatus
from app.models.generation import GenerationAttempt, GenerationSubmission
from app.models.media import MediaNode
from app.providers.base import ProviderError

logger = logging.getLogger("manju.reconciler")


def _now_seconds() -> datetime:
    """秒级时间戳。

    `generation_attempts.last_reconciled_at` 是 MySQL `DATETIME`（无小数秒），
    写入时**会四舍五入**：直接写 `utcnow()`（带微秒）可能被进位到「下一秒」，
    于是 `_due` 在这 1 秒内都判定为「还不到时间」，本轮复查被静默跳过。
    这里先截断到秒，写进去的值就是读回来的值。
    """
    return utcnow().replace(microsecond=0)


def _due(attempt: GenerationAttempt, interval_seconds: int) -> bool:
    if attempt.last_reconciled_at is None:
        return True
    return attempt.last_reconciled_at < utcnow() - timedelta(
        seconds=max(0, interval_seconds)
    )


def reconcilable_attempts(db: Session, *, limit: int) -> list[GenerationAttempt]:
    from app.services import generation_service

    return list(
        db.exec(
            select(GenerationAttempt)
            .where(
                GenerationAttempt.provider_task_id.is_not(None),  # type: ignore[union-attr]
                GenerationAttempt.physical_status.in_(  # type: ignore[union-attr]
                    generation_service.RECONCILABLE_PHYSICAL_STATUSES
                ),
                GenerationAttempt.operation == Operation.video_draw,
            )
            .order_by(GenerationAttempt.created_at)
            .limit(limit)
        ).all()
    )


def reconcile_provider_tasks(db: Session, *, limit: int = 4) -> int:
    """复查一轮在途供应商任务，返回实际推进了状态的条数。"""
    processed = 0
    for attempt in reconcilable_attempts(db, limit=limit):
        if not _due(attempt, settings.GENERATION_RECONCILE_INTERVAL_SECONDS):
            continue
        try:
            if reconcile_attempt(db, attempt.id):
                processed += 1
        except Exception:  # noqa: BLE001 - 单条失败不能拖垮整轮
            db.rollback()
            logger.exception("reconcile failed for attempt %s", attempt.id)
    return processed


def reconcile_attempt(db: Session, attempt_id: str) -> bool:
    from app.providers import registry
    from app.services import generation_service, media_ops

    attempt = db.get(GenerationAttempt, attempt_id)
    if attempt is None:
        return False
    submission = db.get(GenerationSubmission, attempt.submission_id)
    if submission is None:
        return False

    plan_id = generation_service._payload(submission).get("mediaNodeId") or submission.target_id
    plan = db.get(MediaNode, str(plan_id)) if plan_id else None
    if plan is None or plan.media_kind != MediaKind.video_plan:
        _give_up(db, attempt, submission, "供应商任务对应的视频方案节点已不存在")
        return True

    snapshot = media_ops._snapshot(attempt)
    duration_seconds = int(snapshot.get("durationSeconds") or 5)
    resolution = str(snapshot.get("resolution") or "720p")
    index = int(snapshot.get("index") or 0)
    product_label = str(
        generation_service._payload(submission).get("productLabel") or "mini"
    )

    try:
        provider, model_config = registry.resolve_video_provider(
            db,
            product_label=product_label,
            model_config_id=attempt.model_config_id,
        )
        result = provider.resume(
            task_id=str(attempt.provider_task_id), product_label=product_label
        )
    except ProviderError as exc:
        _note_failure(db, attempt, exc.code, exc.message)
        if (
            exc.code == "PROVIDER_TIMEOUT_UNKNOWN"
            and (attempt.reconcile_count or 0) < settings.GENERATION_RECONCILE_MAX_ATTEMPTS
        ):
            # 供应商仍可能在做：保持 unknown，下一轮继续查，绝不重新提交。
            db.commit()
            return True
        _give_up(db, attempt, submission, f"供应商任务无法恢复：{exc.code} {exc.message}")
        return True
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        reloaded = db.get(GenerationAttempt, attempt_id)
        if reloaded is None:
            return False
        _note_failure(db, reloaded, "PROVIDER_UNAVAILABLE", str(exc)[:2000])
        db.commit()
        return True

    canvas_id = media_ops.deliver_video_result(
        db,
        submission,
        plan=plan,
        attempt=attempt,
        result=result,
        duration_seconds=duration_seconds,
        resolution=resolution,
        index=index,
        model_config=model_config,
    )
    payload = generation_service._payload(submission)
    produced = list(payload.get("producedNodeIds") or [])
    if canvas_id and canvas_id not in produced:
        produced.append(canvas_id)
    generation_service.update_payload(submission, producedNodeIds=produced)

    # 恢复成功 = 这次提交真正交付了：逻辑状态从 running 收敛到 succeeded。
    submission.logical_status = LogicalStatus.succeeded
    submission.finished_at = utcnow()
    submission.error_code = ""
    submission.error_message = ""
    submission.recovery_reason = "供应商任务由 reconciler 恢复并完成交付"
    submission.claimed_at = None
    submission.claimed_by = ""
    submission.heartbeat_at = None
    touch(submission)
    db.add(submission)
    db.commit()
    return True


def _note_failure(db: Session, attempt: GenerationAttempt, code: str, message: str) -> None:
    attempt.reconcile_count = (attempt.reconcile_count or 0) + 1
    attempt.last_reconciled_at = _now_seconds()
    attempt.error_code = code
    attempt.error_message = message
    touch(attempt)
    db.add(attempt)


def _give_up(
    db: Session,
    attempt: GenerationAttempt,
    submission: GenerationSubmission,
    reason: str,
) -> None:
    """确认不可恢复：收敛为终态，但绝不自动重新提交供应商任务。"""
    from app.services import generation_service

    # 先把 attempt 移出可复查集合，再收 convergence：
    # 否则 converge_failure 会看到「仍有在途任务」，把 submission 永远留在 running。
    attempt.provider_status_raw = attempt.physical_status.value
    attempt.physical_status = PhysicalStatus.failed
    attempt.error_code = "PROVIDER_TASK_UNRESOLVED"
    attempt.error_message = reason[:2000]
    touch(attempt)
    db.add(attempt)
    db.commit()
    generation_service.converge_failure(
        db,
        submission.id,
        code="PROVIDER_TASK_UNRESOLVED",
        message=f"{reason}；请人工确认供应商计费后再决定是否重新提交",
        recovery_reason="供应商任务确认不可恢复，已停止自动复查",
    )


__all__ = ["reconcilable_attempts", "reconcile_attempt", "reconcile_provider_tasks"]
