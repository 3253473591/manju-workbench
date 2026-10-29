"""P1-3 SSE：事件总线、断线补发、连接上限、以及状态变化是否真的被推出去。

流用 **async generator 直接驱动**（`event_stream`），不用 `TestClient` 读无终止响应 ——
后者会把响应缓冲到超时，属于测法错误而不是实现错误。HTTP 层只验"鉴权在流开始前完成"。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from manju.api.events import event_stream
from manju.config import Settings
from manju.constants import SSE_MAX_CONNECTIONS_PER_USER
from manju.events import BUS, Bus
from tests.conftest import login


def _first(gen, count: int) -> list[str]:
    async def collect() -> list[str]:
        frames: list[str] = []
        try:
            for _ in range(count):
                frames.append(await gen.__anext__())
        finally:
            await gen.aclose()
        return frames

    return asyncio.run(collect())


# ---------------------------------------------------------------- 总线


def test_publish_and_subscribe_receives_event() -> None:
    bus = Bus()
    queue, replay, resync = bus.subscribe(1)
    assert replay == [] and resync is False
    event = bus.publish(1, "job.updated", {"job": {"id": 7, "state": "running"}})
    assert event.seq == 1
    assert queue.get_nowait() is event


def test_replay_after_last_event_id() -> None:
    bus = Bus()
    first = bus.publish(1, "job.updated", {"n": 1})
    second = bus.publish(1, "job.updated", {"n": 2})
    _, replay, resync = bus.subscribe(1, after=first.seq)
    assert [item.seq for item in replay] == [second.seq]
    assert resync is False


def test_resync_when_buffer_dropped_events() -> None:
    bus = Bus(keep=3)
    for index in range(6):
        bus.publish(1, "job.updated", {"n": index})
    _, replay, resync = bus.subscribe(1, after=1)  # seq 1 已被冲出环形缓冲
    assert resync is True
    assert len(replay) == 3


def test_buffer_is_bounded_and_isolated_per_project() -> None:
    bus = Bus(keep=5)
    for index in range(12):
        bus.publish(1, "job.updated", {"n": index})
    bus.publish(2, "job.updated", {"n": 0})
    _, replay_one, _ = bus.subscribe(1)
    _, replay_two, _ = bus.subscribe(2)
    assert len(replay_one) == 5
    assert replay_one[-1].data["n"] == 11  # 留最新的
    assert len(replay_two) == 1  # 项目之间互不影响


def test_slow_subscriber_is_capped_not_unbounded() -> None:
    bus = Bus(keep=4)
    queue, _, _ = bus.subscribe(1)
    for index in range(10):
        bus.publish(1, "job.updated", {"n": index})
    assert queue.qsize() == 4  # 满队丢最老的，慢客户端靠 Last-Event-ID 补


def test_frame_format_matches_protocol() -> None:
    bus = Bus()
    event = bus.publish(3, "clip.updated", {"clip": {"id": 9, "status": "review"}})
    frame = event.frame()
    assert frame.startswith(f"id: {event.seq}\nevent: clip.updated\ndata: ")
    data = json.loads(frame.split("data: ", 1)[1].split("\n", 1)[0])
    assert data == {"clip": {"id": 9, "status": "review"}}
    assert event.stamped()["ts"].endswith("Z")


def test_connection_limit_per_user() -> None:
    bus = Bus()
    assert all(bus.open_connection(7) for _ in range(SSE_MAX_CONNECTIONS_PER_USER))
    assert bus.open_connection(7) is False
    bus.close_connection(7)
    assert bus.open_connection(7) is True


# ---------------------------------------------------------------- 流


def test_stream_starts_with_connected_then_replays() -> None:
    bus = Bus()
    bus.publish(1, "job.updated", {"job": {"id": 5, "state": "running"}})
    bus.publish(1, "clip.updated", {"clip": {"id": 6, "status": "review"}})
    frames = _first(event_stream(bus, 1, None), 3)
    assert frames[0].startswith(": connected")
    assert "event: job.updated" in frames[1] and '"state":"running"' in frames[1]
    assert "event: clip.updated" in frames[2]


def test_stream_sends_resync_first_when_buffer_gap() -> None:
    bus = Bus(keep=2)
    for index in range(5):
        bus.publish(1, "job.updated", {"n": index})
    frames = _first(event_stream(bus, 1, 1), 2)
    assert frames[0].startswith(": connected")
    assert frames[1].startswith("event: resync")


def test_stream_unsubscribes_when_closed() -> None:
    bus = Bus()
    bus.publish(1, "job.updated", {"n": 1})
    _first(event_stream(bus, 1, None), 2)
    # 关闭后不该再留着订阅者：否则每个断开的浏览器都漏一份队列
    assert bus._subscribers.get(1, []) == []  # noqa: SLF001（观测内部状态是这条用例的目的）


# ---------------------------------------------------------------- HTTP / 事件源


@pytest.fixture
def bus() -> Bus:
    BUS.reset()
    yield BUS
    BUS.reset()


def test_events_requires_login(client: TestClient, world: dict) -> None:
    response = client.get(f"/api/v1/projects/{world['project'].id}/events")
    assert response.status_code == 401


def test_generate_publishes_job_event(
    client: TestClient, world: dict, settings: Settings, bus: Bus
) -> None:
    """状态机变更必须推事件，否则前端还得轮询。"""
    login(client, world["owner"], settings)
    generated = client.post(
        f"/api/v1/clips/{world['clip'].id}/generate?mode=auto",
        json={"res": "draft", "budget_limit": "12.000000"},
        headers={"Idempotency-Key": "sse-1", "If-Match": f'"{world["clip"].version}"'},
    )
    assert generated.status_code == 202, generated.text
    _, replay, _ = bus.subscribe(world["project"].id)
    assert any(event.name == "job.updated" for event in replay)
