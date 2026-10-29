"""运行时开关：某条 provider 通道"现在能不能用"。

分两层，别混：
- **配置**（`Settings`）：接的是谁、用哪个模型、key 是什么 —— 来自环境变量，进程内不可变；
- **运行态**（这张表）：这条通道此刻是否被停用、谁停的、为什么 —— 可变，落在共享存储里。

为什么不用进程内字典：多副本时管理员在 A 上停用、B 还照常花钱，这叫"假开关"。
表在 DB 里，所有实例读到同一份；写入带 `updated_at`，前端能显示"什么时候被谁停的"。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from manju.errors import ApiError, ErrorCode
from manju.models import RuntimeFlag

CHANNELS = ("text", "vision", "video", "image")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _check_channel(channel: str) -> None:
    if channel not in CHANNELS:
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"field": "channel", "allowed": list(CHANNELS)})


def flags(session: Session) -> dict[str, RuntimeFlag | None]:
    rows = session.scalars(select(RuntimeFlag).where(RuntimeFlag.kind == "provider")).all()
    by_key = {row.key_name: row for row in rows}
    return {channel: by_key.get(channel) for channel in CHANNELS}


def is_enabled(session: Session, channel: str) -> bool:
    _check_channel(channel)
    row = session.scalar(
        select(RuntimeFlag).where(RuntimeFlag.kind == "provider", RuntimeFlag.key_name == channel)
    )
    return row is None or bool(row.enabled)


def assert_enabled(session: Session, channel: str) -> None:
    """过了这道闸才允许调上游。停用时 503 且带上"谁、为什么、什么时候"。"""
    _check_channel(channel)
    row = session.scalar(
        select(RuntimeFlag).where(RuntimeFlag.kind == "provider", RuntimeFlag.key_name == channel)
    )
    if row is None or row.enabled:
        return
    raise ApiError(
        ErrorCode.PROVIDER_DISABLED,
        {
            "channel": channel,
            "reason": row.reason,
            "actor_id": row.actor_id,
            "since": row.updated_at.isoformat(timespec="seconds"),
        },
    )


def set_flag(session: Session, channel: str, *, enabled: bool, reason: str | None, actor_id: int) -> RuntimeFlag:
    """幂等：重复设成同一状态也返回 200，但 `updated_at` 会刷新（"最后一次确认"的时间）。"""
    _check_channel(channel)
    row = session.scalar(
        select(RuntimeFlag).where(RuntimeFlag.kind == "provider", RuntimeFlag.key_name == channel).with_for_update()
    )
    if row is None:
        row = RuntimeFlag(kind="provider", key_name=channel, enabled=int(enabled))
        session.add(row)
    row.enabled = int(enabled)
    row.reason = reason if not enabled else None
    row.actor_id = actor_id
    row.updated_at = _now()
    session.commit()
    return row
