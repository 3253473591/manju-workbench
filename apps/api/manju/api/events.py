"""SSE 事件流（`02` §7，P1-3）。

这个端点**故意不挂 `get_session` 依赖**：依赖在响应结束后才关闭，而 SSE 连接一挂就是几分钟，
等于把一条 DB 连接钉死在流上（§7.3 明令禁止）。所以权限只查一次、查完立刻释放 session。

流本身抽成 `event_stream()`：`TestClient` 会把无终止的响应读到超时，直接驱动 async generator
才是可测的写法（`test_events.py` 就是这么测的）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from manju.config import COOKIE_NAME
from manju.constants import SSE_PING, SSE_POLL
from manju.deps import PathId, project_for_read
from manju.errors import ApiError, ErrorCode
from manju.events import BUS, Bus, Event
from manju.models import User
from manju.security import read_token

router = APIRouter(tags=["events"])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def _authorize(request: Request, project_id: int) -> User:
    """一次性的权限检查：开短 session，查完就关。"""
    settings = request.app.state.settings
    user_id = read_token(request.cookies.get(COOKIE_NAME), settings)
    with request.app.state.sessionmaker() as session:
        user = session.get(User, user_id)
        if user is None:
            raise ApiError(ErrorCode.AUTH_REQUIRED)
        if not user.is_active:
            raise ApiError(ErrorCode.AUTH_DISABLED)
        project_for_read(session, user, project_id)
        return user


def _last_event_id(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() else None


def _comment(text: str) -> str:
    return f": {text}\n\n"


async def event_stream(
    bus: Bus,
    project_id: int,
    after: int | None,
    request: Request | None = None,
) -> AsyncIterator[str]:
    """连接首帧 → 补发 → 实时帧（进程内直推）+ 跨副本轮询兜底 → 每 `SSE_PING` 秒一次 ping。

    为什么两条来源：订阅队列给**本副本**毫秒级延迟；日志轮询给**别的副本**发的事件兜底，
    延迟上限是 `SSE_POLL`。两者按 `seq` 去重。
    """
    queue, buffer_replay, buffer_resync = bus.subscribe(project_id, after=after)
    if bus.has_store:
        replay, resync = await asyncio.to_thread(bus.replay, project_id, after)
    else:
        replay, resync = buffer_replay, buffer_resync
    last = replay[-1].seq if replay else (after or 0)
    timeout = SSE_POLL if bus.has_store else SSE_PING
    last_ping = time.monotonic()
    try:
        yield _comment("connected")
        if resync:
            yield f"event: resync\ndata: {json.dumps({'reason': 'buffer_gap'})}\n\n"
        for event in replay:
            yield event.frame() + "\n"
        while True:
            try:
                event: Event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                if bus.has_store:
                    for polled in await asyncio.to_thread(bus.poll, project_id, last):
                        if polled.seq <= last:
                            continue
                        last = polled.seq
                        yield polled.frame() + "\n"
                if time.monotonic() - last_ping >= SSE_PING:
                    last_ping = time.monotonic()
                    yield _comment("ping")
                    if request is not None and await request.is_disconnected():
                        break
                continue
            if event.seq <= last:  # 日志已经推过这一条
                continue
            last = event.seq
            yield event.frame() + "\n"
    finally:
        bus.unsubscribe(project_id, queue)


@router.get("/projects/{project_id}/events")
async def project_events(project_id: PathId, request: Request) -> StreamingResponse:
    user = _authorize(request, project_id)
    if not BUS.open_connection(user.id):
        raise ApiError(ErrorCode.RATE_LIMITED, {"reason": "too_many_sse_connections"})

    async def guarded() -> AsyncIterator[str]:
        try:
            async for frame in event_stream(BUS, project_id, _last_event_id(request.headers.get("last-event-id")), request):
                yield frame
        finally:
            BUS.close_connection(user.id)

    return StreamingResponse(guarded(), media_type="text/event-stream", headers=SSE_HEADERS)
