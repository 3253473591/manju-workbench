"""job 状态机、幂等与执行（`02` §6.1 / `03` §8）。

这一层不认识 HTTP，只认 job 与 provider；上层（`api/jobs.py`）负责形状与错误码。
状态迁移表在这里，非法跳转抛 `JOB_STATE_INVALID`。

预算与抢占的两条硬规矩（`03` §4.1 / §6.5）：
- 预算检查在**调用之前**，穿线就 `paused_budget`，绝不先花钱再判断；
- 抢占用 `FOR UPDATE SKIP LOCKED`，多 worker 不会拿到同一个 job。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time  # 别用 `time`：会遮住 time 模块（perf_counter 就是它）
from decimal import Decimal
from pathlib import Path
from secrets import token_hex

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from manju import clip_rules, lessons, review, seed
from manju.events import BUS
from manju import runtime
from manju.config import Settings
from manju.constants import (
    BUDGET_JOB_MAX,
    BUDGET_LIMIT_MIN,
    BUDGET_PROJECT_DAILY,
    TRACE_KEEP,
    VISION_SCORE_GATE,
)
from manju.errors import ApiError, ErrorCode
from manju.models import Asset, Clip, Episode, Job, JobTrace, Project, Revision
from manju.providers import Provider, ProviderError, ProviderResult, make_provider

VIDEO = "video"
logger = logging.getLogger("manju.jobs")


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _emit(project_id: int, name: str, **data: object) -> None:
    """推一条项目事件（`02` §7.2）。发布是线程安全的，worker 线程里也能直接调。"""
    BUS.publish(project_id, name, data)


def _emit_job(project_id: int, job: Job) -> None:
    _emit(
        project_id,
        "job.updated",
        job={"id": job.id, "state": job.state, "attempts": job.attempts, "cost_used": str(job.cost_used)},
    )


def _emit_clip(project_id: int, clip: Clip) -> None:
    _emit(project_id, "clip.updated", clip={"id": clip.id, "status": clip.status, "version": clip.version})

# `02` §6.1 的迁移图；同状态视为幂等
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"done", "failed", "paused_budget", "cancelled"}),
    "waiting_confirm": frozenset({"queued", "cancelled"}),
    "failed": frozenset({"queued"}),
    "paused_budget": frozenset({"queued"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_idem_key(user_id: int, header: str) -> str:
    """`03` §8.1：存 `sha256(user_id:头值)`，`user_id` 参与 hash，全局唯一索引即可隔离用户。"""
    return _sha(f"{user_id}:{header}")


def request_hash(payload: dict) -> str:
    """请求体指纹：同 key 异 body 要能判成 `409 IDEM_KEY_REUSED`。"""
    return _sha(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _transition(job: Job, target: str) -> None:
    if target == job.state:
        return
    if target not in TRANSITIONS.get(job.state, frozenset()):
        raise ApiError(ErrorCode.JOB_STATE_INVALID, {"from": job.state, "to": target})


def _project_id(session: Session, clip: Clip) -> int:
    return session.scalar(select(Episode.project_id).where(Episode.id == clip.episode_id))


def _running_video_jobs(session: Session, project_id: int) -> int:
    clip_ids = select(Clip.id).join(Episode, Episode.id == Clip.episode_id).where(Episode.project_id == project_id)
    return session.scalar(
        select(func.count()).select_from(Job)
        .where(
            Job.kind == VIDEO,
            Job.target_kind == "clip",
            Job.target_id.in_(clip_ids),
            Job.state.in_(("queued", "running")),
        )
    )


def _lock_project(session: Session, clip: Clip) -> int:
    project_id = _project_id(session, clip)
    session.execute(select(Project.id).where(Project.id == project_id).with_for_update())
    return project_id


def _project_spent_today(session: Session, project_id: int) -> Decimal:
    """项目当天的花费（UTC 自然日）。数据源只有 `revision.cost`，不重算（`02` §5.7）。"""
    clip_ids = select(Clip.id).join(Episode, Episode.id == Clip.episode_id).where(
        Episode.project_id == project_id
    )
    start = datetime.combine(datetime.now(UTC).date(), dt_time.min)
    spent = session.scalar(
        select(func.coalesce(func.sum(Revision.cost), 0)).where(
            Revision.target_kind == "clip",
            Revision.target_id.in_(clip_ids),
            Revision.created_at >= start,
        )
    )
    return Decimal(spent or 0)


def submit(
    session: Session,
    *,
    user_id: int,
    clip: Clip,
    budget_limit: Decimal,
    mode: str,
    idem_header: str,
    payload: dict,
    provider: str = "mock",
) -> tuple[Job, bool]:
    """提交生成。返回 `(job, replayed)`：重放命中时不再新建、也不再校验预算。"""
    key = derive_idem_key(user_id, idem_header)
    fingerprint = request_hash(payload)
    existing = session.scalar(select(Job).where(Job.idem_key == key))
    if existing is not None:
        if existing.request_hash and existing.request_hash != fingerprint:
            raise ApiError(ErrorCode.IDEM_KEY_REUSED, {"job_id": existing.id})
        return existing, True

    if not BUDGET_LIMIT_MIN <= budget_limit <= BUDGET_JOB_MAX:
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"field": "budget_limit", "max": str(BUDGET_JOB_MAX)})
    project_id = _lock_project(session, clip)
    # 等 project 锁期间，同 key 的首次提交可能已经完成。
    existing = session.scalar(select(Job).where(Job.idem_key == key))
    if existing is not None:
        if existing.request_hash and existing.request_hash != fingerprint:
            raise ApiError(ErrorCode.IDEM_KEY_REUSED, {"job_id": existing.id})
        return existing, True
    runtime.assert_enabled(session, "video")  # 视频通道被停用 → 不建 job、不花钱
    if _running_video_jobs(session, project_id):
        raise ApiError(ErrorCode.PROJECT_BUSY, {"retry_after_s": 5}, headers={"Retry-After": "5"})
    # 项目日预算（`03` §7.3）：到上限就**在提交前** 402，不排进队列再失败。
    # 这一闸以前只有常量和文档、代码里没有 —— 业务逻辑探针撞出来的缺口。
    spent_today = _project_spent_today(session, project_id)
    if spent_today >= BUDGET_PROJECT_DAILY:
        raise ApiError(
            ErrorCode.BUDGET_EXCEEDED,
            {"scope": "project_daily", "spent": str(spent_today), "limit": str(BUDGET_PROJECT_DAILY)},
        )

    job = Job(
        kind=VIDEO,
        target_kind="clip",
        target_id=clip.id,
        state="queued" if mode == "auto" else "waiting_confirm",
        attempts=0,
        max_attempts=3,
        budget_limit=budget_limit,
        cost_used=Decimal(0),
        idem_key=key,
        request_hash=fingerprint,
        mode=mode,
        provider=provider,
        model=None,
        created_at=_now(),
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        # 并发同 key：另一个请求已经插入，读回来重放（`03` §8.2）
        session.rollback()
        existing = session.scalar(select(Job).where(Job.idem_key == key))
        if existing is None:
            raise
        if existing.request_hash and existing.request_hash != fingerprint:
            raise ApiError(ErrorCode.IDEM_KEY_REUSED, {"job_id": existing.id})
        return existing, True
    _emit_job(project_id, job)
    return job, False


def _job_project(session: Session, job: Job) -> int | None:
    """job 指向的工作台项目（目前只有 clip 类 job，其余返回 None 就不推事件）。"""
    if job.target_kind != "clip":
        return None
    clip = session.get(Clip, job.target_id)
    return _project_id(session, clip) if clip is not None else None


def confirm(session: Session, job: Job) -> Job:
    """`waiting_confirm → queued`。确认是**付费动作**：已排队/已跑完时再点一次不能回 200，
    否则客户端会以为又授权了一次付费（`03` §8.2 明确写 409）。"""
    if job.state != "waiting_confirm":
        raise ApiError(ErrorCode.JOB_STATE_INVALID, {"from": job.state, "to": "queued"})
    project_id = _lock_for_queue(session, job)
    _transition(job, "queued")
    if job.state != "queued" and _running_video_jobs(session, project_id):
        raise ApiError(ErrorCode.PROJECT_BUSY, {"retry_after_s": 5}, headers={"Retry-After": "5"})
    job.state = "queued"
    _emit_job(project_id, job)
    return job


def cancel(session: Session, job: Job) -> Job:
    session.refresh(job, with_for_update=True)
    if job.state in ("done", "cancelled"):
        raise ApiError(ErrorCode.JOB_NOT_CANCELLABLE, {"state": job.state})
    _transition(job, "cancelled")
    job.state = "cancelled"
    job.finished_at = _now()
    project_id = _job_project(session, job)
    if project_id is not None:
        _emit_job(project_id, job)
    return job


def retry(session: Session, job: Job) -> Job:
    project_id = _lock_for_queue(session, job)
    if job.state not in ("failed", "paused_budget"):
        raise ApiError(ErrorCode.JOB_STATE_INVALID, {"state": job.state})
    if job.attempts >= job.max_attempts:
        # 提前判，别让 DB 的 ck_job_attempt 炸成 INTERNAL_STATE（`03` §8.2）
        raise ApiError(ErrorCode.JOB_STATE_INVALID, {"reason": "attempts_exhausted", "attempts": job.attempts})
    _transition(job, "queued")
    if _running_video_jobs(session, project_id):
        raise ApiError(ErrorCode.PROJECT_BUSY, {"retry_after_s": 5}, headers={"Retry-After": "5"})
    job.state = "queued"
    job.attempts += 1
    job.error_code = None
    job.error_detail = None
    job.finished_at = None
    _emit_job(project_id, job)
    return job


def _lock_for_queue(session: Session, job: Job) -> int:
    # 与 worker 保持 job → project 的锁顺序，避免确认与产物入仓互相等待。
    session.refresh(job, with_for_update=True)
    clip = session.get(Clip, job.target_id) if job.target_kind == "clip" else None
    if clip is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    project_id = _lock_project(session, clip)
    return project_id


def claim_next(session: Session, worker_id: str) -> Job | None:
    job = session.execute(
        select(Job)
        .where(Job.state == "queued")
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        return None
    job.state = "running"
    job.locked_by = f"{worker_id}#{token_hex(4)}"
    job.started_at = _now()
    job.heartbeat_at = _now()
    session.flush()
    return job


def _guarded(session: Session, job: Job, **values) -> bool:
    """状态写入必须带执行令牌（`03` §8.3）：非持有者影响 0 行就停手。"""
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.locked_by == job.locked_by, Job.state == "running")
        .values(**values)
    )
    return result.rowcount == 1


def record_trace(
    session: Session, job: Job, step: str, *, verdict: str | None = None, ms: int | None = None, **detail: object
) -> None:
    """记一条执行时间线，并把该 job 裁到最近 `TRACE_KEEP` 条（`02` §5.4）。

    时间线写在当前事务里：`run_job` 抛异常时这些行会随 savepoint 一起回滚，
    所以失败路径由 `_fail_job` 在回滚之后补一条 —— 那一条才是"为什么失败"的锚点。
    """
    session.add(
        JobTrace(job_id=job.id, ts=_now(), step=step, verdict=verdict, ms=ms, detail=dict(detail) or None)
    )
    session.flush()
    stale = session.scalars(
        select(JobTrace.id).where(JobTrace.job_id == job.id).order_by(JobTrace.id.desc()).offset(TRACE_KEEP)
    ).all()
    if stale:
        session.execute(delete(JobTrace).where(JobTrace.id.in_(stale)))
        session.flush()


def _resolve_output(session: Session, settings: Settings, clip: Clip, output_path: str) -> Asset:
    """把产物落成 asset：同项目同 sha 就复用，不重复入仓（`01` §4.5 的 uk_asset_sha）。"""
    path = (settings.media_root / output_path).resolve()
    root = settings.media_root.resolve()
    if root not in path.parents and path != root:
        raise ApiError(ErrorCode.INTERNAL, {"reason": "产物越出 MEDIA_ROOT"})
    if not path.is_file():
        raise ApiError(ErrorCode.ASSET_FETCH_FAILED, {"path": output_path})
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    sha = digest.hexdigest()
    project_id = _project_id(session, clip)
    existing = session.scalar(select(Asset).where(Asset.project_id == project_id, Asset.sha256 == sha))
    if existing is not None:
        return existing
    asset = Asset(
        project_id=project_id,
        kind="video",
        # 统一正斜杠：种子资产是正斜杠，Windows 上 Path 会给反斜杠，混着存前后端都要兼容两套
        path=Path(output_path).as_posix(),
        sha256=sha,
        bytes=path.stat().st_size,
        source="gen",
        created_at=_now(),
    )
    session.add(asset)
    try:
        # 同项目视频 job 是串行的，但"查同 sha → 插"仍不是原子的（出图等其它入口也会插资产）。
        # 竞败方回读对手那一行：产物内容一样，复用即可。
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        session.expunge(asset)
        existing = session.scalar(select(Asset).where(Asset.project_id == project_id, Asset.sha256 == sha))
        if existing is None:
            raise
        return existing
    # 真出片没有 seed 那一步：补宽高/时长/海报，否则镜头墙取图 404（真 provider 才暴露的缺口）
    seed.enrich_asset(asset, settings.media_root)
    session.flush()
    return asset


def run_job(session: Session, job: Job, settings: Settings, provider: Provider | None = None) -> Job:
    """执行一个已 claim 的 job。失败落在 job 终态上，不抛给调用方（worker 不该被单个任务打死）。"""
    token = job.locked_by
    session.refresh(job, with_for_update=True)
    if job.state != "running" or not token or job.locked_by != token:
        return job
    clip = session.get(Clip, job.target_id)
    if clip is None or job.target_kind != "clip":
        return _fail_job(session, job, ErrorCode.NOT_FOUND)
    provider = provider or make_provider(settings, job.provider or settings.provider)
    record_trace(session, job, "claim", provider=provider.name, mode=job.mode, attempts=job.attempts)
    project_id = _project_id(session, clip)
    _emit_job(project_id, job)
    seconds = Decimal(clip.duration_s)
    estimate = provider.estimate(seconds)
    if estimate > job.budget_limit - job.cost_used or job.cost_used >= job.budget_limit:
        record_trace(
            session, job, "budget_precheck", verdict="paused",
            estimate=str(estimate), cost_used=str(job.cost_used), budget_limit=str(job.budget_limit),
        )
        _guarded(
            session,
            job,
            state="paused_budget",
            error_code="BUDGET_EXCEEDED",
            error_detail={"budget_limit": str(job.budget_limit), "cost_used": str(job.cost_used)},
            finished_at=_now(),
            locked_by=None,
        )
        session.flush()
        session.refresh(job)
        _emit_job(project_id, job)
        return job

    started = time.perf_counter()
    try:
        result = provider.video(prompt=clip.prompt or "", seconds=seconds, model=job.model or "seedance-2.0-mini")
    except ProviderError as exc:
        record_trace(
            session, job, "provider", verdict="error", ms=_elapsed_ms(started),
            code="PROVIDER_UNAVAILABLE", retryable=exc.retryable,
        )
        record_trace(session, job, "decision", verdict="failed", code="PROVIDER_UNAVAILABLE")
        _guarded(
            session,
            job,
            state="failed",
            error_code="PROVIDER_UNAVAILABLE",
            error_detail={"message": str(exc), "retryable": exc.retryable, "raw": exc.raw},
            finished_at=_now(),
            locked_by=None,
        )
        session.flush()
        session.refresh(job)
        _emit_job(project_id, job)
        return job
    record_trace(
        session, job, "provider", verdict="ok", ms=_elapsed_ms(started),
        model=result.model, cost=str(result.cost), tokens=result.input_tokens,
    )

    revision = Revision(
        target_kind="clip",
        target_id=clip.id,
        prompt=clip.prompt,
        negative=clip.negative,
        params_json={"mode": "draft", "res": "720p", "provider": provider.name},
        provider=provider.name,
        # 新 take 的父级就是当前那一次产物：重发链路靠这条串起来（`01` §4.6）
        parent_revision_id=clip.current_revision_id,
        model_requested=job.model or result.model,
        model_used=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        video_seconds=result.video_seconds,
        unit_price=result.unit_price,
        cost=result.cost,
        latency_ms=result.latency_ms,
        motion_json=result.motion,
        created_at=_now(),
    )
    session.add(revision)
    session.flush()
    spent = job.cost_used + result.cost
    _guarded(session, job, cost_used=spent, model=result.model)
    session.flush()
    try:
        clip_rules.enforce_cost(revision.cost, revision.unit_price, revision.video_seconds, revision.input_tokens)
    except ApiError as exc:
        # provider 的账单自相矛盾（cost ≠ unit_price × 数量）。上游确实返回了、钱确实花了，
        # 所以 revision 与 cost_used 一律照落（`03` §3 计费铁律），只把 job 停在 failed 等人工核账 ——
        # 把 enforce_cost 放在落库之前，会让最该被记账的那次调用恰好被回滚掉
        return _fail_job(session, job, exc.code, project_id=project_id)
    if spent > job.budget_limit:
        # 花钱发生在预算检查之后：上游确实返回了、钱确实花了，所以 **revision 照落**，
        # 只把 job 停在 paused_budget 等人工提额（`03` §3 计费铁律：台账宁可偏高，不可漏记）
        record_trace(session, job, "decision", verdict="paused_budget", cost_used=str(spent))
        _guarded(
            session,
            job,
            state="paused_budget",
            model=result.model,
            error_code="BUDGET_EXCEEDED",
            error_detail={"budget_limit": str(job.budget_limit), "cost_used": str(spent)},
            finished_at=_now(),
            locked_by=None,
        )
        session.flush()
        session.refresh(job)
        _emit_job(project_id, job)
        _emit(project_id, "cost.updated", cost_used=str(spent))
        return job

    try:
        # 产物入仓/质检失败只回滚后处理；provider 已返回的费用不能跟着丢失。
        with session.begin_nested():
            return _finish_video(session, job, clip, revision, result, settings, provider)
    except Exception as exc:
        logger.exception("任务后处理失败 job_id=%s", job.id)
        code = exc.code if isinstance(exc, ApiError) else ErrorCode.INTERNAL_STATE
        return _fail_job(session, job, code, project_id=project_id)


def _fail_job(session: Session, job: Job, code: ErrorCode, *, project_id: int | None = None) -> Job:
    _guarded(session, job, state="failed", error_code=code.value, finished_at=_now(), locked_by=None)
    session.flush()
    # 失败锚点：`run_job` 抛异常时事务已回滚，这条在回滚之后补，才留得住
    record_trace(session, job, "decision", verdict="failed", code=code.value)
    session.refresh(job)
    if project_id is not None:
        _emit_job(project_id, job)
    return job


def _finish_video(session: Session, job: Job, clip: Clip, revision: Revision,
                  result: ProviderResult, settings: Settings, provider: Provider | None) -> Job:
    project_id = _project_id(session, clip)
    asset = _resolve_output(session, settings, clip, result.output_path) if result.output_path else None
    revision.output_asset_id = asset.id if asset else None
    if asset is not None:
        asset.revision_id = revision.id
    record_trace(
        session, job, "asset", verdict="ok" if asset else "missing",
        asset_id=asset.id if asset else None, path=result.output_path,
    )

    # 内容状态跟着产物走：新 take 一律回到 `review` 等判定（`02` §6.2）。
    if clip.status == "draft":
        clip_rules.enforce_transition(clip.status, "prompted")
        clip.status = "prompted"
    if clip.status in ("prompted", "rejected"):
        clip_rules.enforce_transition(clip.status, "generating")
        clip.status = "generating"
    clip_rules.enforce_transition(clip.status, "review")
    clip.status = "review"
    clip.version += 1
    clip.current_revision_id = revision.id
    clip.updated_at = _now()

    # 质检：动线报告落 revision；动线判不了的内容级判据再交视觉模型，仍判不了就保持 unknown
    review_started = time.perf_counter()
    media = settings.media_root / asset.path if asset else None
    report = evaluate(clip, media) if media else review.judge(clip.acceptance_json, None, "无产物，跳过质检")
    if media is not None and settings.vision:
        # **视觉走自己的通道**（PROVIDER），不是执行出片的那条（PROVIDER_VIDEO）：
        # 两条通道拆开之后复用 provider 会让真视觉退化回 mock（实测踩过）
        report = review.judge_with_vision(
            clip.acceptance_json, report, media, make_provider(settings), workdir=settings.media_root / ".review"
        )
    score = review.score_of(report.results)
    record_trace(
        session, job, "review", verdict=report.verdict, ms=_elapsed_ms(review_started),
        failed=[item["id"] for item in report.results if item["result"] == "fail"],
        unknown=[item["id"] for item in report.results if item["result"] == "unknown"],
        score=score,
        degraded=report.degraded,
    )
    revision.acceptance_result_json = report.results or None
    if report.motion is not None:
        revision.motion_json = report.motion.as_json()
    session.flush()

    spent = job.cost_used
    if report.verdict == "fail":
        failure_type, fix, symptom = attribute(report)
        if job.mode == "auto" and fix and job.attempts + 1 < job.max_attempts:
            # 全能模式：写记忆 + 定向改写 + 自己再排一次（`03` §4.1，≤3 次且预算前置）
            lessons.remember(
                session,
                scope=f"project:{_project_id(session, clip)}",
                failure_type=failure_type,
                symptom=symptom,
                fix=fix,
                evidence_revision_id=revision.id,
            )
            clip.prompt = f"{clip.prompt or ''}\n{fix}".strip()
            record_trace(
                session, job, "decision", verdict="retry",
                failure_type=failure_type, fix=fix, attempts=job.attempts + 1,
            )
            _guarded(
                session,
                job,
                state="queued",
                attempts=job.attempts + 1,
                cost_used=spent,
                error_code="ACCEPTANCE_FAIL",
                error_detail={"failed": [item["id"] for item in report.results if item["result"] == "fail"], "fix": fix},
                locked_by=None,
                finished_at=None,
            )
            session.flush()
            session.refresh(job)
            _emit_job(project_id, job)
            _emit_clip(project_id, clip)
            return job
        # 归因不出来 / 次数用尽 → 转人工，绝不空转
        record_trace(
            session, job, "decision", verdict="failed",
            failure_type=failure_type, fix=fix, attempts=job.attempts,
        )
        _guarded(
            session,
            job,
            state="failed",
            cost_used=spent,
            model=result.model,
            error_code="ACCEPTANCE_FAIL" if fix else "ESCALATED",
            error_detail={
                "failed": [item for item in report.results if item["result"] == "fail"],
                "fix": fix,
                "attempts": job.attempts,
            },
            finished_at=_now(),
            locked_by=None,
        )
        session.flush()
        session.refresh(job)
        _emit_job(project_id, job)
        _emit_clip(project_id, clip)
        return job

    judged_pass = report.verdict == "pass" and all(item["result"] == "pass" for item in report.results)
    # 一条验收点都没有 ≠ 判不了：没有可机检项，就不该把出片永远堵在 review（第二集终剪文档
    # 没有验收点段，实测会一直 awaiting_human）。留痕写进 trace，不假装"质检通过"。
    unaudited = not report.results
    # 数值门：判据都 pass、但均分低于阈值 → 默认模式留给人看；auto 模式允许多试一次
    low_pending = judged_pass and score is not None and score < VISION_SCORE_GATE
    if low_pending:
        low_fix = "质检均分偏低：把主体放大、给镜头明确位移与情绪落点，不要用静态构图凑时长"
        if job.mode == "auto" and job.attempts + 1 < job.max_attempts:
            lessons.remember(
                session,
                scope=f"project:{_project_id(session, clip)}",
                failure_type="low_score",
                symptom=f"质检均分 {score} < {VISION_SCORE_GATE}",
                fix=low_fix,
                evidence_revision_id=revision.id,
            )
            clip.prompt = f"{clip.prompt or ''}\n{low_fix}".strip()
            record_trace(
                session, job, "decision", verdict="retry",
                failure_type="low_score", fix=low_fix, score=score, threshold=VISION_SCORE_GATE,
                attempts=job.attempts + 1,
            )
            _guarded(
                session,
                job,
                state="queued",
                attempts=job.attempts + 1,
                cost_used=spent,
                error_code="ACCEPTANCE_FAIL",
                error_detail={"low_score": score, "threshold": VISION_SCORE_GATE, "fix": low_fix},
                locked_by=None,
                finished_at=None,
            )
            session.flush()
            session.refresh(job)
            _emit_job(project_id, job)
            _emit_clip(project_id, clip)
            return job
    if (judged_pass or unaudited) and not low_pending:
        # 只有**全部**判据都真判过才算通过。带 unknown 的留在 review 等人看：
        # "判不了"不等于"通过"（`02` §6.1 的 done 条件是全部验收点 pass）
        clip_rules.enforce_transition(clip.status, "approved")
        clip.status = "approved"
    record_trace(
        session, job, "decision",
        verdict=(
            "low_score" if low_pending
            else "approved" if judged_pass
            else "no_criteria" if unaudited
            else "awaiting_human"
        ),
        clip_status=clip.status,
        criteria=len(report.results),
        score=score,
        threshold=VISION_SCORE_GATE if low_pending else None,
    )
    _guarded(
        session,
        job,
        state="done",
        cost_used=spent,
        model=result.model,
        finished_at=_now(),
        locked_by=None,
    )
    session.flush()
    session.refresh(job)
    _emit_job(project_id, job)
    _emit_clip(project_id, clip)
    _emit(project_id, "cost.updated", cost_used=str(job.cost_used))
    return job


def evaluate(clip: Clip, asset_path: Path) -> review.Report:
    """跑一次动线质检。测试会 monkeypatch 它 —— 真跑 vscan 需要仓库外的脚本，不能进单测。"""
    motion, note = review.scan(asset_path)
    return review.judge(clip.acceptance_json, motion, note)


def attribute(report: review.Report) -> tuple[str, str | None, str]:
    """把"哪条判据没过"翻译成"下次改什么"。返回 `(failure_type, fix, symptom)`。

    `fix` 为 None 表示归因不出来 —— 上层据此转人工而不是瞎重试（`03` §4.2）。
    """
    failed = [item for item in report.results if item["result"] == "fail"]
    if not failed:
        return "acceptance_fail", None, "无失败判据"
    symptoms = "；".join(f"{item['id']} {item['evidence']}" for item in failed)
    motion = report.motion
    if motion is not None and (motion.freeze or motion.tail_collapse):
        return "motion_freeze", "收尾不要衰减：最后 1 秒保持与前段同等的画面位移，禁止静止定格", symptoms
    if any("定镜" in item["evidence"] for item in failed):
        return "acceptance_fail", "开场第一帧就要有主体并在推进，不要定镜空景", symptoms
    if motion is not None and motion.avg < 1.5:
        return "motion_freeze", "整段画面几乎不动：给每个镜头写明主体位移或机位运动", symptoms
    if motion is None:
        return "acceptance_fail", None, symptoms
    return "acceptance_fail", None, symptoms


def run_pending(session: Session, settings: Settings, worker_id: str, provider: Provider | None = None) -> int:
    """把一个 queued job 跑到终态。worker 循环与测试都调它。"""
    job = claim_next(session, worker_id)
    if job is None:
        return 0
    try:
        with session.begin_nested():
            run_job(session, job, settings, provider)
    except Exception as exc:
        logger.exception("任务执行失败 job_id=%s", job.id)
        code = exc.code if isinstance(exc, ApiError) else ErrorCode.INTERNAL_STATE
        _fail_job(session, job, code)
    session.commit()
    return 1


def reap_stale(session: Session, worker_id: str) -> int:
    """worker 崩溃后把僵死的 running 放回队列（`03` §2 / §5：心跳 120 s）。

    只回收**别人的**令牌：本进程正在跑的 job 心跳是新的，不该被自己抢回。
    """
    result = session.execute(
        update(Job)
        .where(
            Job.state == "running",
            Job.heartbeat_at < heartbeat_timeout(),
            Job.locked_by.is_not(None),
        )
        .values(state="queued", locked_by=None)
    )
    session.commit()
    return result.rowcount


def heartbeat_timeout() -> datetime:
    """僵死判定的阈值（120 s，`03` §5）。"""
    return _now() - timedelta(seconds=120)
