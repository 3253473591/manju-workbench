"""`/admin/providers`：provider 的**配置视图** + **连通性自检** + **运行时开关**。

两层分开：
- 配置（key / model / base_url）来自环境变量，进程内不可改 —— 不做"面板点一下换 key"，那是假开关；
- 运行态（这条通道现在能不能用）是可变的，落在 `runtime_flag` 表里，多副本共享，
  停用后所有调用点（steps / images / jobs.submit）在花钱前就 503。

视频通道最近才接真（Seedance），视图里如实标 `real`，不假装接了没接的厂商。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from manju.config import Settings
from manju import runtime
from manju.deps import current_user, get_session, get_settings, require_global_owner
from manju.errors import ApiError, ErrorCode
from manju.models import User
from manju.providers import ProviderError, make_provider, provider_status

router = APIRouter(tags=["admin"])


class ProviderChannel(BaseModel):
    model: str
    real: bool
    enabled: bool = True
    reason: str | None = None
    updated_at: str | None = None
    actor_id: int | None = None


class ProviderOut(BaseModel):
    provider: str
    base_url: str | None
    text: ProviderChannel
    vision: ProviderChannel
    video: ProviderChannel
    image: ProviderChannel
    timeout_s: int
    note: str


class ProviderCheckOut(BaseModel):
    ok: bool
    provider: str
    model: str | None
    latency_ms: int | None
    detail: str


def _status_with_flags(session: Session, settings: Settings) -> dict:
    status = provider_status(settings)
    flags = runtime.flags(session)
    for channel in runtime.CHANNELS:
        row = flags.get(channel)
        status[channel]["enabled"] = True if row is None else bool(row.enabled)
        status[channel]["reason"] = None if row is None else row.reason
        status[channel]["updated_at"] = None if row is None else row.updated_at.isoformat(timespec="seconds")
        status[channel]["actor_id"] = None if row is None else row.actor_id
    return status


@router.get("/admin/providers")
def get_providers(
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ProviderOut:
    require_global_owner(user)
    status = _status_with_flags(session, settings)
    return ProviderOut(
        **status,
        note=(
            "配置来自环境变量，进程内不可改；换了 key 要重启。"
            "`enabled` 是**运行时开关**（存 `runtime_flag` 表，多副本共享）：停用后 steps / 出图 / 生成在花钱前直接 503。"
            "text/vision 走 /chat/completions；video 走 Seedance（PROVIDER_VIDEO=ark）；image 走出图通道（PROVIDER_IMAGE=images）。"
        ),
    )


@router.post("/admin/providers/check")
def check_provider(
    session_settings: Settings = Depends(get_settings),
    user: User = Depends(current_user),
) -> ProviderCheckOut:
    """真的打一次上游（最小请求），用于"我给了 key，能不能用"这一问。"""
    require_global_owner(user)
    settings = session_settings
    if settings.provider == "mock":
        return ProviderCheckOut(
            ok=True,
            provider="mock",
            model=None,
            latency_ms=0,
            detail="当前是 mock 模式：不需要 key，也不会产生费用。配 PROVIDER=openai 再点一次才是真检查。",
        )
    provider = make_provider(settings)
    started = time.perf_counter()
    try:
        result = provider.text(stage="health", context="只回复 JSON：{\"ok\":true}")
    except ProviderError as exc:
        return ProviderCheckOut(
            ok=False,
            provider=provider.name,
            model=settings.provider_text_model or None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail=f"{exc}（retryable={exc.retryable}）",
        )
    except ValueError as exc:
        raise ApiError(ErrorCode.INTERNAL_STATE, {"reason": str(exc)}) from exc
    return ProviderCheckOut(
        ok=True,
        provider=provider.name,
        model=result.model,
        latency_ms=result.latency_ms,
        detail=f"上游可用：返回 {len(result.payload)} 个字段，tokens {result.input_tokens}→{result.output_tokens}",
    )


class ProviderStateIn(BaseModel):
    enabled: bool
    reason: str | None = None


@router.post("/admin/providers/{channel}/state")
def set_provider_state(
    channel: str,
    payload: ProviderStateIn,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ProviderOut:
    """停用/启用一条通道。**只影响运行态**：key 和模型仍来自环境变量。

    停用是"立刻生效"的：已排队的 job 不受影响（它在 submit 时已经过了闸），
    但新的 steps / 出图 / 生成会 503 —— 这正是"止血开关"该有的语义。
    """
    require_global_owner(user)
    if not payload.enabled and not (payload.reason or "").strip():
        raise ApiError(ErrorCode.VALIDATION_SHAPE, {"field": "reason", "reason": "停用必须写原因"})
    runtime.set_flag(session, channel, enabled=payload.enabled, reason=payload.reason, actor_id=user.id)
    status = _status_with_flags(session, settings)
    return ProviderOut(**status, note="已更新运行态；配置本身仍来自环境变量。")
