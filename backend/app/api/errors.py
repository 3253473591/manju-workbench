"""Exception handlers：把内部异常统一转成契约里的错误结构。

契约见 docs/architecture/exception-handling.md 与 docs/openapi.yaml 的 ErrorResponse。
"""

from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.errors import AppError
from app.core.utils import to_iso, utcnow

logger = logging.getLogger("manju.errors")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


def error_payload(
    code: str, message: str, details: dict | None, request_id: str
) -> dict:
    return {
        "success": False,
        "error": {"code": code, "message": message, "details": details or {}},
        "requestId": request_id,
        "timestamp": to_iso(utcnow()),
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message, exc.details, _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = {
            "fields": [
                {
                    "location": ".".join(str(p) for p in err.get("loc", [])),
                    "message": err.get("msg", ""),
                    "type": err.get("type", ""),
                }
                for err in exc.errors()
            ]
        }
        return JSONResponse(
            status_code=400,
            content=error_payload(
                "INVALID_REQUEST", "请求参数不合法", details, _request_id(request)
            ),
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(
        request: Request, exc: IntegrityError
    ) -> JSONResponse:
        logger.warning("integrity error on %s: %s", request.url.path, exc.orig)
        return JSONResponse(
            status_code=409,
            content=error_payload(
                "BUSINESS_RULE_VIOLATION",
                "数据冲突，请刷新后重试",
                {},
                _request_id(request),
            ),
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("database error on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content=error_payload("INTERNAL_ERROR", "服务内部错误", {}, _request_id(request)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 响应不泄露堆栈；日志里保留完整栈便于排查。
        logger.error(
            "unhandled error on %s: %s\n%s",
            request.url.path,
            exc,
            traceback.format_exc(),
        )
        return JSONResponse(
            status_code=500,
            content=error_payload("INTERNAL_ERROR", "服务内部错误", {}, _request_id(request)),
        )
