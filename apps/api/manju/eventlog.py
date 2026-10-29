"""事件日志的存取（SSE 跨副本）。

为什么需要它：进程内总线只在本副本可见。两个副本时，A 上跑的 worker 发布的事件，
B 上的浏览器连接收不到 —— 表现是"任务卡在排队中，刷新一下才更新"。
用 DB 表做**权威序列**，任意副本都能按 `id > last` 补上。

边界：
- 只用于补发/续传，保留 `EVENT_KEEP_DAYS` 天（不是审计日志，钱与终态看 `revision` / `job`）；
- 每次发布一次 `INSERT`，量级与 job 状态变化同阶，不是每帧视频；
- 读按 `(project_id, id)` 索引，最多取 `SSE_BUFFER` 条。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from manju.constants import EVENT_KEEP_DAYS, EVENT_PRUNE_EVERY, SSE_BUFFER
from manju.models import EventLog


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    ts: datetime
    name: str
    data: dict


class EventStore:
    """薄薄一层：`append` 写一行拿 id 当 seq，`read` 按 seq 增量取。"""

    def __init__(self, sessionmaker: sessionmaker[Session]) -> None:
        self._sessionmaker = sessionmaker
        self._since_prune = 0

    def append(self, project_id: int, name: str, data: dict) -> tuple[int, datetime]:
        with self._sessionmaker() as session:
            row = EventLog(project_id=project_id, name=name, payload=data, created_at=_now())
            session.add(row)
            session.commit()
            seq, ts = row.id, row.created_at
        self._since_prune += 1
        if self._since_prune >= EVENT_PRUNE_EVERY:
            self._since_prune = 0
            self.prune()
        return seq, ts

    def read(self, project_id: int, after: int | None, limit: int = SSE_BUFFER) -> list[StoredEvent]:
        with self._sessionmaker() as session:
            statement = select(EventLog).where(EventLog.project_id == project_id)
            if after is not None:
                statement = statement.where(EventLog.id > after)
            rows = session.scalars(statement.order_by(EventLog.id).limit(limit)).all()
            return [StoredEvent(seq=row.id, ts=row.created_at, name=row.name, data=row.payload or {}) for row in rows]

    def oldest(self, project_id: int) -> int | None:
        with self._sessionmaker() as session:
            return session.scalar(select(func.min(EventLog.id)).where(EventLog.project_id == project_id))

    def prune(self) -> int:
        """删掉过期行。失败不影响发布（事件已经写进去了，清理是下一次的事）。"""
        cutoff = _now() - timedelta(days=EVENT_KEEP_DAYS)
        try:
            with self._sessionmaker() as session:
                result = session.execute(delete(EventLog).where(EventLog.created_at < cutoff))
                session.commit()
                return result.rowcount or 0
        except SQLAlchemyError:
            return 0

    def clear(self) -> None:
        """测试用：清空（`BUS.reset()` 会调）。"""
        with self._sessionmaker() as session:
            session.execute(delete(EventLog))
            session.commit()
