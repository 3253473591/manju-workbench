"""生成类端点（`02` §3.5 / §5.3）。

校验顺序按 `02` §5.3 写死，其中最容易写反的一条：**幂等键在 `If-Match` 之前**。
客户端超时重试时首次请求已经把 `version` 推进了，先验版本会让重放永远 412 ——
那正好取消掉幂等键唯一的用途。
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import jobs
from manju.api.episodes import _clip_for_write
from manju.config import Settings
from manju.constants import BUDGET_JOB_DEFAULT, IDEM_KEY_PATTERN, TOKENS_PER_VIDEO_SECOND
from manju.deps import (
    Paging,
    PathId,
    check_version,
    current_user,
    cursor_page,
    get_session,
    get_settings,
    paging,
    parse_if_match,
)
from manju.errors import ApiError, ErrorCode
from manju.models import Clip, Job, JobTrace, User
from manju.providers import make_provider
from manju.schemas import GenerateIn, GenerateOut, GeneratePreview, JobOut, Page, TraceOut

router = APIRouter(tags=["jobs"])

RE_IDEM = re.compile(IDEM_KEY_PATTERN)


def _preview(clip: Clip, settings: Settings) -> GeneratePreview:
    prompt = clip.prompt or ""
    seconds = Decimal(clip.duration_s)
    return GeneratePreview(
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        prompt_head=prompt[:80],
        tokens_est=int(TOKENS_PER_VIDEO_SECOND * float(seconds)),
        cost_est=make_provider(settings).estimate(seconds),
        duration_s=clip.duration_s,
    )


def _prepare_generation(
    clip: Clip, payload: GenerateIn, request: Request, *, reroll: bool
) -> tuple[str, str, Decimal, dict]:
    """两条提交路径（首次生成 / 打回重发）的共同部分：幂等键、mode、预算、请求指纹。"""
    idem = request.headers.get("Idempotency-Key")
    # 字符集也要判（`03` §8.1）：只判长度的话，含空格/斜杠/控制字符的键会被静默接收，
    # 而它们在某些客户端/代理链路上本来就到不了，等于让幂等语义看网络脸色
    if not idem or not RE_IDEM.fullmatch(idem):
        raise ApiError(
            ErrorCode.VALIDATION_SHAPE,
            {"header": "Idempotency-Key", "reason": "required, 1-128 chars [A-Za-z0-9._:-]"},
        )
    mode = request.query_params.get("mode", "default")
    if mode not in {"default", "auto"}:
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"field": "mode"})
    # `or` 在这里是错的：`Decimal("0")` 假值，会把用户显式传的 0 悄悄换成默认预算，
    # 于是"预算 0 应该被拒"变成"按 12 元跑"（探针撞出来的）
    budget = payload.budget_limit if payload.budget_limit is not None else BUDGET_JOB_DEFAULT
    body = {"clip_id": clip.id, "res": payload.res, "budget_limit": str(budget), "mode": mode}
    if reroll:
        # 指纹里带上 reroll：同一个幂等键不能既当"首次生成"又当"打回重发"（`03` §8.1）
        body["reroll"] = True
    return idem, mode, budget, body


def _submit_or_replay(
    session: Session,
    user: User,
    clip: Clip,
    idem: str,
    mode: str,
    budget: Decimal,
    body: dict,
    request: Request,
    response: Response,
    settings: Settings,
) -> GenerateOut:
    """`02` §5.3 的顺序：**幂等键在 `If-Match` 之前**（超时重试时版本已被推进）。"""
    key = jobs.derive_idem_key(user.id, idem)
    existing = session.scalar(select(Job).where(Job.idem_key == key))
    if existing is not None:
        if existing.request_hash and existing.request_hash != jobs.request_hash(body):
            raise ApiError(ErrorCode.IDEM_KEY_REUSED, {"job_id": existing.id})
        # 重放不是"新提交"：状态码回到 200（路由默认的 202 只表示新建）
        response.status_code = 200
        response.headers["Idempotency-Replayed"] = "true"
        return GenerateOut(job=JobOut.model_validate(existing), preview=_preview(clip, settings))

    check_version(parse_if_match(request.headers.get("If-Match")), clip.version)
    job, replayed = jobs.submit(
        session,
        user_id=user.id,
        clip=clip,
        budget_limit=budget,
        mode=mode,
        idem_header=idem,
        payload=body,
        # job 落的是**执行它的那条通道**（视频通道单独选：PROVIDER_VIDEO）
        provider=settings.provider_video,
    )
    session.commit()
    if replayed:
        response.status_code = 200
        response.headers["Idempotency-Replayed"] = "true"
    return GenerateOut(job=JobOut.model_validate(job), preview=_preview(clip, settings))


@router.post("/clips/{clip_id}/generate", status_code=202)
def generate_clip(
    clip_id: PathId,
    payload: GenerateIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> GenerateOut:
    clip = _clip_for_write(session, user, clip_id)
    idem, mode, budget, body = _prepare_generation(clip, payload, request, reroll=False)
    return _submit_or_replay(session, user, clip, idem, mode, budget, body, request, response, settings)


@router.post("/clips/{clip_id}/reroll", status_code=202)
def reroll_clip(
    clip_id: PathId,
    payload: GenerateIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> GenerateOut:
    """打回后重发（`02` §6.2：`review ──fail──▶ rejected ──reroll──▶ generating`）。

    与首次生成的差别只有两处：源状态必须是 `rejected`，请求指纹里带 `reroll` ——
    所以同一个幂等键不会把"首次生成"误重放成"重发"。
    """
    clip = _clip_for_write(session, user, clip_id)
    if clip.status != "rejected":
        raise ApiError(
            ErrorCode.JOB_STATE_INVALID,
            {"from": clip.status, "reason": "reroll 只能作用于已打回（rejected）的片段，先点「打回」"},
        )
    idem, mode, budget, body = _prepare_generation(clip, payload, request, reroll=True)
    return _submit_or_replay(session, user, clip, idem, mode, budget, body, request, response, settings)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    return JobOut.model_validate(job)


@router.get("/jobs/{job_id}/trace")
def job_trace(
    job_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[TraceOut]:
    """job 的执行时间线（P1-4）。按发生顺序返回，最多 `TRACE_KEEP` 条。"""
    job = session.get(Job, job_id)
    if job is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    rows = session.scalars(
        select(JobTrace).where(JobTrace.job_id == job_id).order_by(JobTrace.id)
    )
    return [TraceOut.model_validate(row) for row in rows]


@router.get("/jobs")
def list_jobs(
    target_kind: str | None = None,
    target_id: int | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    page: Paging = Depends(paging),
) -> Page[JobOut]:
    stmt = select(Job)
    if target_kind is not None:
        stmt = stmt.where(Job.target_kind == target_kind)
    if target_id is not None:
        stmt = stmt.where(Job.target_id == target_id)
    items, next_cursor = cursor_page(session, stmt, Job, page)
    return Page(items=[JobOut.model_validate(item) for item in items], next_cursor=next_cursor)


def _job_for_write(session: Session, user: User, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    if job.target_kind == "clip":
        _clip_for_write(session, user, job.target_id)
    else:
        raise ApiError(ErrorCode.PERM_DENIED)
    return job


@router.post("/jobs/{job_id}/confirm")
def confirm_job(
    job_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> JobOut:
    job = jobs.confirm(session, _job_for_write(session, user, job_id))
    session.commit()
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> JobOut:
    job = jobs.cancel(session, _job_for_write(session, user, job_id))
    session.commit()
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> JobOut:
    job = jobs.retry(session, _job_for_write(session, user, job_id))
    session.commit()
    return JobOut.model_validate(job)
