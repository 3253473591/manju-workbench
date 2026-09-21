"""生成服务：幂等提交、attempt、文本任务编排与状态推进。

三层状态互不覆盖：
- logical：用户目标（submission.logical_status）
- physical：供应商调用（attempt.physical_status）
- delivery：本地落盘与结构核验（attempt.delivery_status + delivery_records）

幂等：同一 submission_key 的重复请求复用原任务；新用户操作必须换新 key。
"""

from __future__ import annotations

import json
import socket
from datetime import timedelta
from decimal import Decimal

from sqlmodel import Session, select
from sqlalchemy import and_, or_, update

from app.core.errors import AppError
from app.core.prompts import SYSTEM_PROMPTS, content_fingerprint
from app.core.utils import to_iso, utcnow
from app.models.common import touch
from app.models.content import Clip, ContentDocument
from app.models.enums import (
    AttemptTarget,
    BillingStatus,
    DeliveryStatus,
    DocumentKind,
    DocumentStatus,
    LogicalStatus,
    Operation,
    PhysicalStatus,
    PromptSource,
    RevisionSource,
)
from app.models.generation import (
    AttemptLink,
    DeliveryRecord,
    GenerationAttempt,
    GenerationSubmission,
)
from app.models.model_config import ModelConfig, UsageRecord
from app.providers.base import ProviderError, TextResult

TEXT_OPERATIONS = {
    Operation.outline_optimize,
    Operation.setting_generate,
    Operation.script_generate,
    Operation.script_optimize,
    Operation.script_split_clips,
    Operation.clip_optimize,
    Operation.plan_characters,
    Operation.plan_scenes,
    Operation.plan_props,
}

MEDIA_OPERATIONS = {
    Operation.image_plan_create,
    Operation.image_plan_optimize,
    Operation.image_draw,
    Operation.image_derive,
    Operation.video_plan_create,
    Operation.video_plan_optimize,
    Operation.video_draw,
}

ACTIVE_STATUSES = (LogicalStatus.pending, LogicalStatus.running)

#: 已提交给供应商、但还没拿到终态回执的物理状态。这些 attempt 只能靠
#: `provider_task_id` 复查恢复，绝不允许重新提交。
RECONCILABLE_PHYSICAL_STATUSES = (
    PhysicalStatus.accepted,
    PhysicalStatus.running,
    PhysicalStatus.unknown,
)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{id(run_pending)}"


def recover_stale_submissions(db: Session, *, lease_seconds: int | None = None) -> int:
    """Requeue crashed running jobs; an attempt is never silently left as a lock.

    带在途供应商任务的 submission **不参与**租约回收：它由 `provider_reconciler`
    按 `provider_task_id` 复查，重新入队会再提交一次收费任务。

    用**一条批量 UPDATE** 完成，而不是「先 SELECT 出来再逐行改」：后者每行都要经历
    一次读-改-写，在多 worker 并发时与原子领取互相持锁，是死锁的主要来源。
    """
    from app.core.config import settings

    seconds = lease_seconds or settings.GENERATION_LEASE_SECONDS
    cutoff = utcnow() - timedelta(seconds=seconds)
    now = utcnow()

    inflight = (
        select(GenerationAttempt.id)
        .where(
            GenerationAttempt.submission_id == GenerationSubmission.id,
            GenerationAttempt.provider_task_id.is_not(None),  # type: ignore[union-attr]
            GenerationAttempt.physical_status.in_(RECONCILABLE_PHYSICAL_STATUSES),  # type: ignore[union-attr]
        )
        .exists()
    )
    stale = and_(
        GenerationSubmission.logical_status == LogicalStatus.running,
        or_(
            GenerationSubmission.heartbeat_at < cutoff,
            and_(
                GenerationSubmission.heartbeat_at.is_(None),  # type: ignore[union-attr]
                GenerationSubmission.updated_at < cutoff,
            ),
        ),
    )

    requeued = db.exec(
        update(GenerationSubmission)
        .where(stale, ~inflight)
        .values(
            logical_status=LogicalStatus.pending,
            claimed_at=None,
            claimed_by="",
            heartbeat_at=None,
            recovery_count=GenerationSubmission.recovery_count + 1,
            recovery_reason="worker lease expired; requeued after restart",
            updated_at=now,
        )
    )
    # 在途任务的租约到期只记录原因，不改状态（否则会重复计费）。
    db.exec(
        update(GenerationSubmission)
        .where(stale, inflight)
        .values(
            recovery_reason=(
                "lease expired but a provider task is in flight; deferred to reconciler"
            ),
            updated_at=now,
        )
    )
    db.commit()
    return int(getattr(requeued, "rowcount", 0) or 0)


def has_inflight_provider_task(db: Session, submission_id: str) -> bool:
    """该 submission 是否还有等待供应商回执的 attempt。"""
    row = db.exec(
        select(GenerationAttempt.id).where(
            GenerationAttempt.submission_id == submission_id,
            GenerationAttempt.provider_task_id.is_not(None),  # type: ignore[union-attr]
            GenerationAttempt.physical_status.in_(RECONCILABLE_PHYSICAL_STATUSES),  # type: ignore[union-attr]
        )
    ).first()
    return row is not None


# --- 序列化 -------------------------------------------------------------


def submission_out(submission: GenerationSubmission) -> dict:
    return {
        "id": submission.id,
        "operation": submission.operation.value,
        "logicalStatus": submission.logical_status.value,
        "requestedCount": submission.requested_count,
        "episodeId": submission.episode_id,
        "nodeId": submission.node_id,
        "targetType": submission.target_type,
        "targetId": submission.target_id,
        "errorCode": submission.error_code,
        "errorMessage": submission.error_message,
        # 崩溃恢复的审计轨迹：重试次数与最近一次恢复原因必须可查。
        "recoveryCount": submission.recovery_count or 0,
        "recoveryReason": submission.recovery_reason or "",
        "createdAt": to_iso(submission.created_at),
        "finishedAt": to_iso(submission.finished_at),
    }


def usage_out(record: UsageRecord | None) -> dict | None:
    if record is None:
        return None
    return {
        "id": record.id,
        "attemptId": record.attempt_id,
        "inputUnits": record.input_units,
        "outputUnits": record.output_units,
        "unit": json.loads(record.unit_json or "{}"),
        "estimatedAmount": str(record.estimated_amount)
        if record.estimated_amount is not None
        else None,
        "actualAmount": str(record.actual_amount) if record.actual_amount is not None else None,
        "billingStatus": record.billing_status.value,
        "isMock": record.is_mock,
    }


def delivery_out(record: DeliveryRecord | None) -> dict | None:
    if record is None:
        return None
    return {
        "id": record.id,
        "attemptId": record.attempt_id,
        "deliveryStatus": record.delivery_status.value,
        "fileKey": record.file_key,
        "sha256": record.sha256,
        "decodeStatus": record.decode_status,
        "width": record.width,
        "height": record.height,
        "durationMs": record.duration_ms,
        "thumbnailFileKey": record.thumbnail_file_key,
        "verifiedAt": to_iso(record.verified_at),
        "errorMessage": record.error_message,
    }


def attempt_out(db: Session, attempt: GenerationAttempt, *, include_billing: bool) -> dict:
    delivery = db.exec(
        select(DeliveryRecord)
        .where(DeliveryRecord.attempt_id == attempt.id)
        .order_by(DeliveryRecord.created_at.desc())
        .limit(1)
    ).first()
    usage = None
    if include_billing:
        usage = db.exec(
            select(UsageRecord).where(UsageRecord.attempt_id == attempt.id).limit(1)
        ).first()
    return {
        "id": attempt.id,
        "submissionId": attempt.submission_id,
        "attemptNo": attempt.attempt_no,
        "operation": attempt.operation.value,
        "modelConfigId": attempt.model_config_id,
        "contentFingerprint": attempt.content_fingerprint,
        "physicalStatus": attempt.physical_status.value,
        "deliveryStatus": attempt.delivery_status.value,
        "providerRequestId": attempt.provider_request_id,
        "providerTaskId": attempt.provider_task_id,
        "providerStatusRaw": attempt.provider_status_raw,
        "errorCode": attempt.error_code,
        "errorMessage": attempt.error_message,
        "isMock": attempt.is_mock,
        "createdAt": to_iso(attempt.created_at),
        "finishedAt": to_iso(attempt.finished_at),
        "delivery": delivery_out(delivery),
        "usage": usage_out(usage),
    }


def get_submission_or_404(db: Session, submission_id: str) -> GenerationSubmission:
    submission = db.get(GenerationSubmission, submission_id)
    if submission is None:
        raise AppError("RESOURCE_NOT_FOUND", "提交不存在")
    return submission


def list_attempts(db: Session, submission_id: str) -> list[GenerationAttempt]:
    return list(
        db.exec(
            select(GenerationAttempt)
            .where(GenerationAttempt.submission_id == submission_id)
            .order_by(GenerationAttempt.attempt_no)
        ).all()
    )


def get_submission_detail(db: Session, submission_id: str, *, include_billing: bool) -> dict:
    submission = get_submission_or_404(db, submission_id)
    attempts = list_attempts(db, submission.id)
    payload = _payload(submission)
    return {
        "submission": submission_out(submission),
        "attempts": [attempt_out(db, a, include_billing=include_billing) for a in attempts],
        "producedNodeIds": payload.get("producedNodeIds", []),
        "result": payload.get("result"),
    }


def _payload(submission: GenerationSubmission) -> dict:
    try:
        return json.loads(submission.payload_json or "{}")
    except json.JSONDecodeError:
        return {}


def update_payload(submission: GenerationSubmission, **values: object) -> None:
    payload = _payload(submission)
    payload.update(values)
    submission.payload_json = json.dumps(payload, ensure_ascii=False, default=str)


# --- 提交生命周期 -------------------------------------------------------


def create_submission(
    db: Session,
    *,
    submission_key: str,
    operation: Operation,
    episode_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    node_id: str | None = None,
    requested_count: int = 1,
    created_by: str | None = None,
    request_id: str = "",
    payload: dict | None = None,
    lock_key: str | None = None,
    require_idle: bool = True,
    commit: bool = True,
) -> tuple[GenerationSubmission, bool]:
    """返回 (submission, 是否新建)。重复 key 复用原任务；key 相同但请求不同返回 409。"""
    body = payload or {}
    existing = db.exec(
        select(GenerationSubmission).where(
            GenerationSubmission.submission_key == submission_key
        )
    ).first()
    if existing is not None:
        if _payload(existing) != body:
            raise AppError(
                "IDEMPOTENCY_CONFLICT",
                "相同的 Idempotency-Key 对应了不同的请求",
                details={"submissionKey": submission_key},
            )
        return existing, False

    if require_idle and lock_key:
        active = active_submission_for_lock(db, lock_key)
        if active is not None:
            raise AppError(
                "GENERATION_IN_PROGRESS",
                "该节点已有未结束的生成任务",
                details={"nodeId": lock_key, "submissionId": active.id},
            )

    submission = GenerationSubmission(
        submission_key=submission_key,
        operation=operation,
        logical_status=LogicalStatus.pending,
        requested_count=requested_count,
        episode_id=episode_id,
        target_type=target_type,
        target_id=target_id,
        node_id=node_id,
        lock_key=lock_key,
        created_by=created_by,
        request_id=request_id,
        payload_json=json.dumps(body, ensure_ascii=False, default=str),
    )
    db.add(submission)
    if commit:
        db.commit()
    else:
        # 调用方要跟其它写入放进同一个事务；flush 让对象变成 persistent，
        # 这样它才能被 db.refresh / 后续查询看见。
        db.flush()
    db.refresh(submission)
    return submission, True


def submission_by_key(db: Session, submission_key: str) -> GenerationSubmission | None:
    return db.exec(
        select(GenerationSubmission).where(
            GenerationSubmission.submission_key == submission_key
        )
    ).first()


def active_submission_for_lock(db: Session, lock_key: str) -> GenerationSubmission | None:
    """节点级生成锁的**唯一判据**。

    走 `lock_key` 而不是 `node_id`：Clip 生成视频方案这类操作的真实锁对象是
    「Clip 对应的画布节点」，而本次新建的视频方案节点还没来得及成为任何人的键。
    """
    return db.exec(
        select(GenerationSubmission).where(
            GenerationSubmission.lock_key == lock_key,
            GenerationSubmission.logical_status.in_(ACTIVE_STATUSES),  # type: ignore[union-attr]
        )
    ).first()


def active_submission_for_node(db: Session, node_id: str) -> GenerationSubmission | None:
    """发送按钮的锁必须在服务端：这里就是判据。"""
    return active_submission_for_lock(db, node_id)


def list_submissions(db: Session, episode_id: str, *, page: int, page_size: int) -> dict:
    from sqlalchemy import func

    base = select(GenerationSubmission).where(GenerationSubmission.episode_id == episode_id)
    total = db.exec(select(func.count()).select_from(base.subquery())).one()
    rows = db.exec(
        base.order_by(GenerationSubmission.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [submission_out(s) for s in rows],
        "total": int(total or 0),
        "page": page,
        "pageSize": page_size,
    }


def run_submission(db: Session, submission: GenerationSubmission) -> GenerationSubmission:
    """同步执行一次提交。后台 worker 与测试都走这里。"""
    if submission.logical_status in (LogicalStatus.succeeded, LogicalStatus.cancelled):
        return submission
    submission.logical_status = LogicalStatus.running
    submission.claimed_at = utcnow()
    submission.claimed_by = _worker_id()
    submission.heartbeat_at = utcnow()
    touch(submission)
    db.add(submission)
    db.commit()

    try:
        if submission.operation in TEXT_OPERATIONS:
            _run_text_operation(db, submission)
        elif submission.operation in MEDIA_OPERATIONS:
            from app.services import media_ops

            media_ops.run(db, submission)
        else:
            raise AppError("INVALID_REQUEST", f"未实现的操作 {submission.operation.value}")
    except AppError as exc:
        db.rollback()
        return converge_failure(
            db, submission.id, code=exc.code, message=exc.message
        )
    except ProviderError as exc:
        db.rollback()
        return converge_failure(
            db, submission.id, code=exc.code, message=exc.message
        )
    except Exception as exc:  # noqa: BLE001 - every job must converge to a terminal/retryable state
        db.rollback()
        return converge_failure(
            db,
            submission.id,
            code="GENERATION_FAILED",
            message=str(exc)[:2000],
            recovery_reason=f"unhandled exception: {type(exc).__name__}",
        )

    submission = db.get(GenerationSubmission, submission.id)
    submission.logical_status = LogicalStatus.succeeded
    submission.finished_at = utcnow()
    submission.claimed_at = None
    submission.claimed_by = ""
    submission.heartbeat_at = None
    touch(submission)
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return submission


def converge_failure(
    db: Session,
    submission_id: str,
    *,
    code: str,
    message: str,
    recovery_reason: str = "",
) -> GenerationSubmission | None:
    """把提交收敛到终态。

    例外：如果这次提交还握着在途的供应商任务（`provider_task_id` 已落库且物理状态
    仍是 accepted/running/unknown），**不能**标成 failed 就完事——那会让一个已收费
    的供应商任务失去唯一的重查入口。此时保留 running，交给 reconciler 按任务 ID
    复查收敛。
    """
    submission = db.get(GenerationSubmission, submission_id)
    if submission is None:
        return None
    if has_inflight_provider_task(db, submission_id):
        submission.logical_status = LogicalStatus.running
        submission.error_code = code
        submission.error_message = message
        submission.recovery_reason = (
            recovery_reason or "provider task in flight; awaiting reconciler"
        )
        touch(submission)
        db.add(submission)
        db.commit()
        return submission

    submission.logical_status = LogicalStatus.failed
    submission.error_code = code
    submission.error_message = message
    submission.finished_at = utcnow()
    if recovery_reason:
        submission.recovery_reason = recovery_reason
    submission.claimed_at = None
    submission.claimed_by = ""
    submission.heartbeat_at = None
    touch(submission)
    db.add(submission)
    db.commit()
    return submission


def run_pending(db: Session, *, limit: int = 5) -> int:
    """把 pending 的提交推进一轮。先推进状态再判锁，避免锁永久卡住。"""
    recover_stale_submissions(db)
    pending = list(
        db.exec(
            select(GenerationSubmission)
            .where(GenerationSubmission.logical_status == LogicalStatus.pending)
            .order_by(GenerationSubmission.created_at)
            .limit(limit)
        ).all()
    )
    claimed: list[GenerationSubmission] = []
    # Claim in this transaction before doing provider work.  UPDATE ... WHERE
    # status=pending gives each worker a deterministic lease even without SKIP LOCKED.
    for candidate in pending:
        now = utcnow()
        result = db.exec(
            update(GenerationSubmission)
            .where(
                GenerationSubmission.id == candidate.id,
                GenerationSubmission.logical_status == LogicalStatus.pending,
            )
            .values(
                logical_status=LogicalStatus.running,
                claimed_at=now,
                claimed_by=_worker_id(),
                heartbeat_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            continue
        current = db.get(GenerationSubmission, candidate.id)
        if current is not None:
            claimed.append(current)
    db.commit()
    for submission in claimed:
        # run_submission is idempotent for an already claimed row and keeps
        # the existing exception convergence path.
        run_submission(db, submission)
    return len(claimed)


# --- attempt ------------------------------------------------------------


def start_attempt(
    db: Session,
    submission: GenerationSubmission,
    *,
    operation: Operation,
    input_snapshot: dict,
    fingerprint: str,
    model_config: ModelConfig | None = None,
    is_mock: bool = False,
    attempt_no: int | None = None,
) -> GenerationAttempt:
    if attempt_no is None:
        attempt_no = len(list_attempts(db, submission.id)) + 1
    attempt = GenerationAttempt(
        submission_id=submission.id,
        attempt_no=attempt_no,
        operation=operation,
        model_config_id=model_config.id if model_config else None,
        input_snapshot_json=json.dumps(input_snapshot, ensure_ascii=False, default=str),
        content_fingerprint=fingerprint,
        physical_status=PhysicalStatus.reserved,
        delivery_status=DeliveryStatus.pending,
        is_mock=is_mock,
    )
    db.add(attempt)
    db.flush()
    return attempt


def link_attempt(
    db: Session, attempt: GenerationAttempt, target_type: AttemptTarget, target_id: str
) -> None:
    existing = db.exec(
        select(AttemptLink).where(
            AttemptLink.attempt_id == attempt.id,
            AttemptLink.target_type == target_type,
            AttemptLink.target_id == target_id,
        )
    ).first()
    if existing is not None:
        return
    db.add(AttemptLink(attempt_id=attempt.id, target_type=target_type, target_id=target_id))


def finish_attempt_success(
    db: Session,
    attempt: GenerationAttempt,
    *,
    provider_request_id: str = "",
    provider_task_id: str | None = None,
    provider_status_raw: str = "succeeded",
    raw_meta: dict | None = None,
    delivery_status: DeliveryStatus = DeliveryStatus.verified,
    usage: dict | None = None,
    usage_context: dict | None = None,
    model_config: ModelConfig | None = None,
) -> None:
    attempt.physical_status = PhysicalStatus.succeeded
    attempt.delivery_status = delivery_status
    attempt.provider_request_id = provider_request_id
    if provider_task_id:
        attempt.provider_task_id = provider_task_id
    attempt.provider_status_raw = provider_status_raw
    attempt.provider_response_meta_json = json.dumps(raw_meta or {}, ensure_ascii=False, default=str)
    attempt.finished_at = utcnow()
    touch(attempt)
    db.add(attempt)
    if usage is not None:
        record_usage(
            db, attempt, usage, model_config=model_config, context=usage_context
        )
    db.flush()


def finish_attempt_failure(
    db: Session,
    attempt: GenerationAttempt,
    *,
    code: str,
    message: str,
    physical_status: PhysicalStatus = PhysicalStatus.failed,
) -> None:
    attempt.physical_status = physical_status
    attempt.delivery_status = DeliveryStatus.failed
    attempt.error_code = code
    attempt.error_message = message
    attempt.finished_at = utcnow()
    touch(attempt)
    db.add(attempt)
    db.flush()


def mark_delivery_verified(db: Session, attempt: GenerationAttempt) -> None:
    """文本交付完成：结构已校验且业务正文已持久化。"""
    attempt.delivery_status = DeliveryStatus.verified
    touch(attempt)
    db.add(attempt)
    db.flush()


def mark_model_verified(
    db: Session, attempt: GenerationAttempt, actual_model: str = ""
) -> None:
    """真实调用成功后才写核验时间与供应商返回的实际模型标识。

    这是「未核验的档位不能伪装可用」的落地点：`/meta` 的 realAvailable 只看这个字段。
    mock 不参与，绝不把 mock 当成核验。
    """
    if attempt.is_mock or not attempt.model_config_id:
        return
    from app.models.model_config import ModelConfig

    config = db.get(ModelConfig, attempt.model_config_id)
    if config is None:
        return
    config.verified_at = utcnow()
    if actual_model:
        config.actual_model = actual_model
    touch(config)
    db.add(config)
    db.flush()


def record_usage(
    db: Session,
    attempt: GenerationAttempt,
    usage: dict,
    *,
    model_config: ModelConfig | None = None,
    context: dict | None = None,
) -> UsageRecord:
    """用量与费用。计价口径交给 `services.pricing`，这里只负责落库。

    缺失的用量或未登记的单价一律记 `unknown` / `None`，**不默认 0**——
    0 会被读成「免费」，实际只是未知。
    """
    from app.services import pricing

    context = context or {}
    input_units = usage.get("input_tokens", usage.get("input_images"))
    output_units = usage.get("output_tokens", usage.get("completion_tokens"))

    result = pricing.calculate(
        model_config,
        usage=usage,
        resolution=str(context.get("resolution") or ""),
        includes_video_input=bool(context.get("includes_video_input")),
        output_pixels=context.get("output_pixels"),
        is_mock=attempt.is_mock,
    )

    record = UsageRecord(
        attempt_id=attempt.id,
        input_units=int(input_units) if input_units is not None else None,
        output_units=int(output_units) if output_units is not None else None,
        unit_json=json.dumps(
            {"usage": usage, "context": context, "breakdown": result.breakdown},
            ensure_ascii=False,
            default=str,
        ),
        estimated_amount=result.estimated,
        actual_amount=result.amount,
        billing_status=result.status,
        is_mock=attempt.is_mock,
        note=result.note,
    )
    db.add(record)
    db.flush()
    return record


def record_delivery(
    db: Session,
    attempt: GenerationAttempt,
    *,
    file_key: str,
    sha256: str,
    decode_status: str,
    width: int | None = None,
    height: int | None = None,
    duration_ms: int | None = None,
    thumbnail_file_key: str = "",
    delivery_status: DeliveryStatus = DeliveryStatus.verified,
) -> DeliveryRecord:
    record = DeliveryRecord(
        attempt_id=attempt.id,
        delivery_status=delivery_status,
        file_key=file_key,
        sha256=sha256,
        decode_status=decode_status,
        width=width,
        height=height,
        duration_ms=duration_ms,
        thumbnail_file_key=thumbnail_file_key,
        verified_at=utcnow() if delivery_status == DeliveryStatus.verified else None,
    )
    db.add(record)
    attempt.delivery_status = delivery_status
    db.add(attempt)
    db.flush()
    return record


# --- 文本任务 -----------------------------------------------------------


def call_text(
    db: Session,
    submission: GenerationSubmission,
    *,
    system_key: str,
    user_prompt: str,
    expect: str,
    input_snapshot: dict,
    context: dict | None = None,
    target_type: AttemptTarget = AttemptTarget.canvas_node,
    target_id: str | None = None,
    model_config_id: str | None = None,
) -> tuple[dict, GenerationAttempt]:
    from app.providers import registry

    provider, model_config = registry.resolve_text_provider(
        db, model_config_id=model_config_id
    )
    from app.core.config import settings

    is_mock = settings.is_mock
    attempt = start_attempt(
        db,
        submission,
        operation=submission.operation,
        input_snapshot=input_snapshot,
        fingerprint=content_fingerprint(system_key, user_prompt, expect),
        model_config=model_config,
        is_mock=is_mock,
    )
    db.flush()

    try:
        result: TextResult = provider.generate(
            system_prompt=SYSTEM_PROMPTS.get(system_key, ""),
            user_prompt=user_prompt,
            expect=expect,
            json_mode=True,
            context=context or {},
        )
    except ProviderError as exc:
        finish_attempt_failure(db, attempt, code=exc.code, message=exc.message)
        db.commit()
        raise

    payload = parse_json_payload(result.text)
    actual_model = str(result.raw_meta.get("model") or "")
    finish_attempt_success(
        db,
        attempt,
        provider_request_id=result.provider_request_id,
        provider_task_id=result.provider_task_id,
        raw_meta=result.raw_meta,
        usage={
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        },
        # 文本交付 = 结构校验通过并已持久化，二者在调用方完成后再置 verified
        delivery_status=DeliveryStatus.pending,
        model_config=model_config,
    )
    mark_model_verified(db, attempt, actual_model)
    if target_id:
        link_attempt(db, attempt, target_type, target_id)
    db.commit()
    return payload, attempt


def parse_json_payload(text: str) -> dict:
    """结构化任务必须能解析；失败时保留失败 attempt，不把原始响应写入业务正文。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if "\n" in stripped:
            stripped = stripped.split("\n", 1)[1]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AppError(
            "PROVIDER_REJECTED",
            "模型返回的内容不是合法 JSON，已保留失败记录",
            details={"preview": text[:200]},
        ) from exc
    if not isinstance(payload, dict):
        raise AppError("PROVIDER_REJECTED", "模型返回的 JSON 顶层不是对象")
    return payload


def _run_text_operation(db: Session, submission: GenerationSubmission) -> None:
    from app.services import text_ops

    handler = text_ops.HANDLERS.get(submission.operation)
    if handler is None:
        raise AppError("INVALID_REQUEST", f"未实现的操作 {submission.operation.value}")
    handler(db, submission)


__all__ = [
    "ACTIVE_STATUSES",
    "MEDIA_OPERATIONS",
    "RECONCILABLE_PHYSICAL_STATUSES",
    "TEXT_OPERATIONS",
    "active_submission_for_lock",
    "active_submission_for_node",
    "attempt_out",
    "converge_failure",
    "create_submission",
    "finish_attempt_failure",
    "finish_attempt_success",
    "get_submission_detail",
    "get_submission_or_404",
    "has_inflight_provider_task",
    "link_attempt",
    "list_attempts",
    "list_submissions",
    "parse_json_payload",
    "record_delivery",
    "record_usage",
    "recover_stale_submissions",
    "run_pending",
    "run_submission",
    "start_attempt",
    "submission_by_key",
    "submission_out",
    "update_payload",
]
