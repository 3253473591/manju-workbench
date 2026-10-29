"""应用装配：启动校验、请求守卫、错误封装、路由。

生命周期内按 `03` §12.1 逐项校验，缺一项就打印后 `exit 1` —— 宁可起不来，也不要带病跑
（最典型的是单价缺失：成本恒为 0，静默错账）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from secrets import token_hex

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from manju.api import api
from manju.config import Settings, config_problems, load_settings
from manju.constants import BODY_MAX_BYTES
from manju.db import make_engine
from manju.errors import ErrorCode, error_response
from manju.errors import ApiError
from manju.eventlog import EventStore
from manju.events import BUS
from manju.providers import provider_status

logger = logging.getLogger("manju")

_HTTP_TO_CODE = {
    401: ErrorCode.AUTH_REQUIRED,
    403: ErrorCode.PERM_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
}
_RANGE_TYPES = (
    "greater_than",
    "less_than",
    "string_too_long",
    "string_too_short",
    "too_long",
    "too_short",
    # 数值字符串形态（03 §6.2）与邮箱格式（03 §2）都归 VALIDATION_RANGE
    "decimal_format",
    "decimal_digits",
    "invalid_email",
)


def _startup_problems(settings: Settings) -> list[str]:
    problems = config_problems(settings)
    if problems:
        return problems
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if not revision:
            problems.append("数据库未迁移（alembic_version 为空）")
    except SQLAlchemyError as exc:
        problems.append(f"数据库不可用：{exc}")
    finally:
        engine.dispose()
    return problems


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        problems = _startup_problems(settings)
        if problems:
            for problem in problems:
                logger.error("启动校验失败：%s", problem)
            raise SystemExit(1)
        engine = make_engine(settings.database_url, settings.db_echo)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker(engine, expire_on_commit=False)
        # SSE 总线要拿到本进程的事件循环：worker 在 to_thread 里 publish，必须跨线程投递
        BUS.bind_loop(asyncio.get_running_loop())
        # 跨副本补发：事件落 `event_log`，任意副本都能按 seq 续传。
        # 测试里 `app.state.sessionmaker` 被换成 `lambda: nullcontext(session)`（事务回滚隔离），
        # 那种工厂不适合让总线自己 commit，所以只对真 sessionmaker 绑 store。
        BUS.bind_store(EventStore(app.state.sessionmaker) if isinstance(app.state.sessionmaker, sessionmaker) else None)
        status = provider_status(settings)
        logger.warning(
            "provider=%s ｜ text=%s(%s) ｜ vision=%s(%s) ｜ video=%s(%s) —— 视频通道仍是 mock 回放，别当成真出片",
            status["provider"],
            status["text"]["model"], "真调用" if status["text"]["real"] else "mock",
            status["vision"]["model"], "真调用" if status["vision"]["real"] else "mock",
            status["video"]["model"], "真调用" if status["video"]["real"] else "mock",
        )
        stopping = asyncio.Event()
        worker = asyncio.create_task(_worker_loop(app.state.sessionmaker, settings, stopping)) if settings.worker else None
        try:
            yield
        finally:
            if worker is not None:
                stopping.set()
                await worker
            engine.dispose()

    app = FastAPI(title="漫剧工作台", version="0.1.0", lifespan=lifespan)
    app.include_router(api)
    _install_guards(app, settings)
    _install_handlers(app)
    return app


async def _worker_loop(factory, settings: Settings, stopping: asyncio.Event) -> None:
    """进程内 worker：每 2 s 捞一次 queued job。

    同步 SQLAlchemy 放 `to_thread`，别阻塞事件循环；单个 job 抛异常不该打死循环
    （`03` §2 的 L5：基础设施异常靠重连/回收，不靠进程重启）。
    """
    worker_id = f"w{os.getpid()}"
    while not stopping.is_set():
        try:
            processed = await asyncio.to_thread(_drain, factory, settings, worker_id)
            if processed:
                continue
        except Exception:  # worker 循环不能死；单个 job 的错误已落在 job 终态上
            logger.exception("worker 循环异常，2 秒后重试")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=2)
        except TimeoutError:
            continue


def _drain(factory, settings: Settings, worker_id: str) -> int:
    from manju import jobs

    with factory() as session:
        jobs.reap_stale(session, worker_id)
        return jobs.run_pending(session, settings, worker_id)


def _install_guards(app: FastAPI, settings: Settings) -> None:
    """请求守卫：request_id、体积、Content-Type、Origin（`02` §1 / `03` §6.7）。"""

    @app.middleware("http")
    async def guard(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-Id") or f"req_{token_hex(8)}"
        try:
            await _guard_body(request, settings)
        except ApiError as error:
            return error_response(request, error)
        response = await call_next(request)
        response.headers.setdefault("X-Request-Id", request.state.request_id)
        return response


async def _guard_body(request: Request, settings: Settings) -> None:
    if request.method not in {"POST", "PATCH", "PUT", "DELETE"}:
        return
    raw_length = request.headers.get("content-length")
    length = int(raw_length) if raw_length and raw_length.isdigit() else 0
    if length > BODY_MAX_BYTES:
        raise ApiError(ErrorCode.PAYLOAD_TOO_LARGE, {"limit": BODY_MAX_BYTES})
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.csrf_origins:
        # 只有浏览器会带 Origin；curl/测试不带 → 放行。表单类 CSRF 已被下面的
        # Content-Type 检查挡掉：非 JSON 的有体请求一律 415。
        raise ApiError(ErrorCode.PERM_DENIED, {"reason": "origin_not_allowed", "origin": origin})
    chunks = []
    size = 0
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                size += len(chunk)
                if size > BODY_MAX_BYTES:
                    raise ApiError(ErrorCode.PAYLOAD_TOO_LARGE, {"limit": BODY_MAX_BYTES})
                if size and content_type != "application/json":
                    raise ApiError(ErrorCode.UNSUPPORTED_MEDIA, {"content_type": content_type or None})
                if chunk:
                    chunks.append(chunk)
    except TimeoutError as exc:
        raise ApiError(ErrorCode.VALIDATION_SHAPE, {"reason": "body_read_timeout"}) from exc
    body = b"".join(chunks)
    if body:
        # 非法 UTF-8 在这里挡住：`json.loads` 抛的是 UnicodeDecodeError，FastAPI 只捕
        # JSONDecodeError，漏过去就是 500（`03` §6 要求 422，且 detail 只给位置）
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiError(ErrorCode.VALIDATION_SHAPE, {"reason": "body_not_utf8", "position": exc.start}) from exc
    # BaseHTTPMiddleware 会向下游重放缓存的 body。
    request._body = body


def _install_handlers(app: FastAPI) -> None:
    @app.exception_handler(StaleDataError)
    async def handle_stale_write(request: Request, exc: StaleDataError) -> JSONResponse:
        return error_response(request, ApiError(ErrorCode.STALE_VERSION))

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # detail 只给位置与原因，不回显原文（03 §6.7）
        errors = [
            {"loc": list(item.get("loc", ())), "msg": item.get("msg", ""), "type": item.get("type", "")}
            for item in exc.errors()
        ]
        code = ErrorCode.VALIDATION_RANGE if any(
            str(item.get("type", "")).startswith(_RANGE_TYPES) for item in errors
        ) else ErrorCode.VALIDATION_SHAPE
        return error_response(request, ApiError(code, {"errors": errors}))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL)
        return error_response(request, ApiError(code, {"status": exc.status_code}))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 request_id=%s", getattr(request.state, "request_id", None))
        return error_response(request, ApiError(ErrorCode.INTERNAL))


app = create_app()
