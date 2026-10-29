"""协作：编辑锁与在线状态。

锁只是**提示**（`00` §3.9）：任何写请求都不因锁失败，`If-Match` 才是硬校验。
时间一律取 DB 的 `NOW(3)`，不用进程时间判过期（`03` §6.4）。
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from manju.constants import LOCK_TTL, PRESENCE_TTL
from manju.deps import can_write_global, current_user, get_session, project_for_read, project_for_write
from manju.errors import ApiError, ErrorCode
from manju.models import Clip, Episode, LockRow, Presence, User
from manju.schemas import HeartbeatIn, LockIn, LockOut, PresenceOut

router = APIRouter(tags=["collab"])


def _db_now(session: Session):
    return session.scalar(select(func.now(3)))


def _assert_lock_writable(session: Session, user: User, payload: LockIn) -> None:
    """`02` §3.8 把锁的权限标成 write：只读用户不该占用编辑提示。

    资源归属一律从 id 反查 project 再判权限（`02` §5.3 第 1 步）。
    """
    if payload.target_kind == "skill_version":
        if not can_write_global(user):
            raise ApiError(ErrorCode.PERM_ADMIN_ONLY)
        return
    if payload.target_kind == "clip":
        clip = session.get(Clip, payload.target_id)
        if clip is None:
            raise ApiError(ErrorCode.NOT_FOUND)
        episode = session.get(Episode, clip.episode_id)
        project_for_write(session, user, episode.project_id)
        return
    episode = session.get(Episode, payload.target_id)
    if episode is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    project_for_write(session, user, episode.project_id)


@router.post("/locks/acquire")
def acquire_lock(
    payload: LockIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> LockOut:
    _assert_lock_writable(session, user, payload)
    now = _db_now(session)
    lock = session.get(LockRow, (payload.target_kind, payload.target_id))
    if lock is not None and lock.user_id != user.id and lock.expires_at > now:
        holder = session.get(User, lock.user_id)
        raise ApiError(
            ErrorCode.LOCK_HELD,
            {
                "holder": holder.email if holder else str(lock.user_id),
                "expires_at": lock.expires_at.isoformat(timespec="milliseconds"),
            },
        )
    expires_at = now + timedelta(seconds=LOCK_TTL)
    if lock is None:
        lock = LockRow(
            target_kind=payload.target_kind,
            target_id=payload.target_id,
            user_id=user.id,
            acquired_at=now,
            expires_at=expires_at,
        )
        session.add(lock)
    else:
        # 同一人重取 = 续期；过期锁可被夺
        lock.user_id = user.id
        lock.acquired_at = now
        lock.expires_at = expires_at
    session.commit()
    return LockOut(
        target_kind=lock.target_kind,
        target_id=lock.target_id,
        user_id=lock.user_id,
        expires_at=lock.expires_at,
    )


@router.post("/locks/release", status_code=204)
def release_lock(
    payload: LockIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    lock = session.get(LockRow, (payload.target_kind, payload.target_id))
    # 不是自己的锁就什么都不做（幂等），抢删别人的锁会把协作指示变成互相踩
    if lock is not None and lock.user_id == user.id:
        session.delete(lock)
        session.commit()


@router.post("/presence/heartbeat")
def heartbeat(
    payload: HeartbeatIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[PresenceOut]:
    project_for_read(session, user, payload.project_id)
    now = _db_now(session)
    session.execute(
        mysql_insert(Presence)
        .values(
            project_id=payload.project_id,
            user_id=user.id,
            focus_target=payload.focus_target,
            last_seen_at=now,
        )
        .on_duplicate_key_update(focus_target=payload.focus_target, last_seen_at=now)
    )
    session.commit()
    cutoff = now - timedelta(seconds=PRESENCE_TTL)
    rows = session.execute(
        select(Presence, User.email)
        .join(User, User.id == Presence.user_id)
        .where(Presence.project_id == payload.project_id, Presence.last_seen_at > cutoff)
        .order_by(Presence.user_id)
    ).all()
    return [
        PresenceOut(user_id=presence.user_id, email=email, focus_target=presence.focus_target)
        for presence, email in rows
    ]
