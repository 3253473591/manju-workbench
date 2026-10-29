"""项目级事件总线（`02` §7，P1-3）。

三条硬约束，决定了这里的形状：

1. **worker 跑在 `to_thread` 里**，所以 `publish()` 必须线程安全 —— 只能通过
   `loop.call_soon_threadsafe` 把事件投进目标事件循环，绝不直接往别人循环的队列里 put；
2. **SSE 不占 DB 连接**（§7.3）：事件从进程内队列出；DB 只做**短连接**的落库与轮询；
3. **跨副本也要能补**：`seq` 由 `event_log.id` 提供（全局自增），别的副本发的事件
   在 `SSE_POLL` 周期内由轮询补上；同一进程仍走订阅队列直推，延迟不变；
4. **刷新要自洽**：带 `Last-Event-ID` 时先从日志补发，缺口过大就发 `resync` 让前端全量刷新。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy.exc import SQLAlchemyError

from manju.constants import SSE_BUFFER, SSE_MAX_CONNECTIONS_PER_USER
from manju.eventlog import EventStore


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Event:
    seq: int
    ts: datetime
    name: str
    data: dict

    def frame(self) -> str:
        """SSE 文本帧（`02` §7.1）。`id` 用全局递增序号，前端拿它做续传。"""
        import json

        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        stamp = self.ts.replace(tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return f"id: {self.seq}\nevent: {self.name}\ndata: {payload}\n"

    def stamped(self) -> dict:
        return {"seq": self.seq, "ts": self.ts.replace(tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"), **self.data}


class Bus:
    """进程内总线。一个项目一份缓冲；订阅者各自一个 `asyncio.Queue`。"""

    def __init__(self, keep: int = SSE_BUFFER) -> None:
        self._keep = keep
        self._lock = Lock()
        self._seq = 0
        self._buffers: dict[int, deque[Event]] = {}
        self._subscribers: dict[int, list[asyncio.Queue[Event]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connections: dict[int, int] = {}
        self._max_connections = SSE_MAX_CONNECTIONS_PER_USER
        self._store: EventStore | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """由 lifespan 调用：worker 线程 publish 时要靠它把事件送回事件循环。"""
        self._loop = loop

    def bind_store(self, store: EventStore | None) -> None:
        """由 lifespan 调用：绑了 store 才有跨副本能力；不绑就是纯进程内（测试/单实例）。"""
        self._store = store

    @property
    def has_store(self) -> bool:
        return self._store is not None

    # ---------------------------------------------------------------- 发布

    def publish(self, project_id: int, name: str, data: dict) -> Event:
        seq, ts = self._next_seq(project_id, name, data)
        with self._lock:
            event = Event(seq=seq, ts=ts, name=name, data=data)
            buffer = self._buffers.setdefault(project_id, deque(maxlen=self._keep))
            buffer.append(event)
            queues = list(self._subscribers.get(project_id, ()))
            loop = self._loop
        for queue in queues:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(_put, queue, event)
            else:
                _put(queue, event)
        return event

    def _next_seq(self, project_id: int, name: str, data: dict) -> tuple[int, datetime]:
        """有 store 就用日志的全局 id；写日志失败就退回本地序号（**发布不能因为日志挂了而失败**）。"""
        if self._store is not None:
            try:
                return self._store.append(project_id, name, data)
            except SQLAlchemyError:  # 日志是旁路：DB 抖一下不该让 job 的状态推送失败
                pass
        with self._lock:
            self._seq += 1
            return self._seq, _now()

    # ---------------------------------------------------------------- 跨副本

    def poll(self, project_id: int, after: int | None) -> list[Event]:
        """从日志里取 `seq > after` 的事件（跨副本兜底）。"""
        if self._store is None:
            return []
        rows = self._store.read(project_id, after)
        return [Event(seq=row.seq, ts=row.ts, name=row.name, data=row.data) for row in rows]

    def replay(self, project_id: int, after: int | None) -> tuple[list[Event], bool]:
        """返回 `(补发事件, 是否需要 resync)`。有 store 时以日志为准，环形缓冲只做单实例兜底。"""
        if self._store is None:
            queue, replay, resync = self.subscribe(project_id, after)
            self.unsubscribe(project_id, queue)  # 只借缓冲补发，不留订阅
            return replay, resync
        events = self.poll(project_id, after)
        if after is None or not events:
            return events, False
        oldest = self._store.oldest(project_id)
        # 最早一条日志都比 after 新很多 → 中间那段已经被清理掉了
        gap = oldest is not None and after + 1 < oldest
        return events, gap

    # ---------------------------------------------------------------- 订阅

    def subscribe(self, project_id: int, after: int | None = None) -> tuple[asyncio.Queue[Event], list[Event], bool]:
        """返回 `(队列, 补发事件, 是否需要 resync)`。"""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._keep)
        with self._lock:
            self._subscribers.setdefault(project_id, []).append(queue)
            buffer = list(self._buffers.get(project_id, ()))
        if after is None:
            return queue, buffer, False
        replay = [event for event in buffer if event.seq > after]
        # 缓冲里最早的事件都比 after 新 → 中间那段已经被冲掉了，只能让前端全量刷新
        resync = bool(buffer) and after < buffer[0].seq - 1
        return queue, replay, resync

    def unsubscribe(self, project_id: int, queue: asyncio.Queue[Event]) -> None:
        with self._lock:
            queues = self._subscribers.get(project_id)
            if not queues:
                return
            try:
                queues.remove(queue)
            except ValueError:
                pass
            if not queues:
                self._subscribers.pop(project_id, None)

    # ---------------------------------------------------------------- 连接数（每用户上限）

    def open_connection(self, user_id: int) -> bool:
        with self._lock:
            used = self._connections.get(user_id, 0)
            if used >= self._max_connections:
                return False
            self._connections[user_id] = used + 1
            return True

    def close_connection(self, user_id: int) -> None:
        with self._lock:
            used = self._connections.get(user_id, 0)
            if used <= 1:
                self._connections.pop(user_id, None)
            else:
                self._connections[user_id] = used - 1

    def reset(self) -> None:
        """测试用：清空缓冲、订阅者与事件日志。"""
        with self._lock:
            self._seq = 0
            self._buffers.clear()
            self._subscribers.clear()
            self._connections.clear()
            store = self._store
        if store is not None:
            store.clear()


def _put(queue: asyncio.Queue[Event], event: Event) -> None:
    """满队就丢最老的：慢客户端不能把内存顶穿，它下次靠 `Last-Event-ID` 补。"""
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        queue.put_nowait(event)


BUS = Bus()
