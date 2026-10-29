"""SSE 跨副本：进程内总线 + 事件日志（`02` §7.4）。

测的是**两个副本**这个场景：副本 A 上的浏览器连接，怎么收到副本 B 上 worker 发布的事件。
做法不依赖网络：两个 `Bus` 共享同一份事件日志；A 的订阅队列收不到 B 的直推，
必须靠 `SSE_POLL` 周期的日志轮询补上 —— 这正是多副本时的真实路径。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from manju.api.events import event_stream
from manju.eventlog import StoredEvent
from manju.events import Bus


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class FakeStore:
    """内存版事件日志：接口与 `EventStore` 一致，避免测试为了测"跨副本"去碰真 DB。"""

    def __init__(self) -> None:
        self.seq = 0
        self.rows: dict[int, list[StoredEvent]] = {}

    def append(self, project_id: int, name: str, data: dict) -> tuple[int, datetime]:
        self.seq += 1
        ts = _now()
        self.rows.setdefault(project_id, []).append(StoredEvent(self.seq, ts, name, data))
        return self.seq, ts

    def read(self, project_id: int, after: int | None, limit: int = 100) -> list[StoredEvent]:
        rows = [row for row in self.rows.get(project_id, []) if after is None or row.seq > after]
        return rows[:limit]

    def oldest(self, project_id: int) -> int | None:
        rows = self.rows.get(project_id)
        return rows[0].seq if rows else None

    def clear(self) -> None:
        self.rows.clear()


@pytest.fixture
def two_replicas(monkeypatch: pytest.MonkeyPatch):
    store = FakeStore()
    replica_a, replica_b = Bus(), Bus()
    replica_a.bind_store(store)
    replica_b.bind_store(store)

    async def take(gen, timeout: float = 2.0) -> str:
        return await asyncio.wait_for(gen.__anext__(), timeout=timeout)

    yield replica_a, replica_b, store, take


def test_event_from_other_replica_reaches_stream(two_replicas) -> None:
    replica_a, replica_b, _store, take = two_replicas

    async def scenario() -> None:
        stream = event_stream(replica_a, 1, None)
        assert await take(stream) == ": connected\n\n"
        # 另一个副本发布：A 的进程内队列收不到，只能靠日志轮询
        replica_b.publish(1, "job.updated", {"job_id": 7, "state": "running"})
        frame = await take(stream)
        assert "event: job.updated" in frame
        assert '"job_id":7' in frame
        await stream.aclose()

    asyncio.run(scenario())


def test_same_replica_event_is_not_delivered_twice(two_replicas) -> None:
    replica_a, _replica_b, _store, take = two_replicas

    async def scenario() -> None:
        stream = event_stream(replica_a, 1, None)
        await take(stream)
        published = replica_a.publish(1, "cost.updated", {"total": "1.00"})
        frame = await take(stream)
        assert f"id: {published.seq}" in frame
        # 直推 + 轮询两条来源：同一条只能出现一次
        with pytest.raises(TimeoutError):
            await take(stream, timeout=1.2)
        await stream.aclose()

    asyncio.run(scenario())


def test_last_event_id_replays_from_log(two_replicas) -> None:
    replica_a, replica_b, _store, take = two_replicas
    first = replica_b.publish(1, "clip.updated", {"n": 1})
    replica_b.publish(1, "clip.updated", {"n": 2})
    replica_b.publish(1, "clip.updated", {"n": 3})

    async def scenario() -> None:
        # 模拟浏览器重连：带着 Last-Event-ID 回到**另一个副本**上
        stream = event_stream(replica_a, 1, first.seq)
        await take(stream)
        frames = [await take(stream), await take(stream)]
        assert ['"n":2' in frames[0], '"n":3' in frames[1]] == [True, True]
        await stream.aclose()

    asyncio.run(scenario())


def test_log_gap_triggers_resync(two_replicas) -> None:
    replica_a, replica_b, store, take = two_replicas
    for index in range(3):
        replica_b.publish(1, "clip.updated", {"n": index})
    # 模拟清理：最早两条被裁掉，客户端却还拿着 seq=1
    store.rows[1] = store.rows[1][2:]

    async def scenario() -> None:
        stream = event_stream(replica_a, 1, 1)
        await take(stream)
        assert "event: resync" in await take(stream)
        await stream.aclose()

    asyncio.run(scenario())
