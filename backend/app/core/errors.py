"""统一业务异常与错误码。契约见 docs/architecture/exception-handling.md。"""

from __future__ import annotations

from typing import Any

# HTTP 状态与业务码的对应关系集中在这里，避免散落各处。
ERROR_STATUS: dict[str, int] = {
    "INVALID_REQUEST": 400,
    "AUTH_REQUIRED": 401,
    "AUTH_INVALID": 401,
    # 账号身份已确认但不允许使用：403 而不是 401，避免前端误判成「需要重新登录」
    "ACCOUNT_DISABLED": 403,
    "REGISTER_USERNAME_EXISTS": 409,
    "PASSWORD_POLICY_FAILED": 400,
    "LAST_ADMIN_REQUIRED": 409,
    "RESOURCE_NOT_FOUND": 404,
    "CONTENT_VERSION_CONFLICT": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "GENERATION_IN_PROGRESS": 409,
    "NODE_TYPE_INVALID": 400,
    "CANVAS_LAYOUT_INVALID": 400,
    "BUSINESS_RULE_VIOLATION": 422,
    "ASSET_REFERENCE_INVALID": 422,
    "FILE_TOO_LARGE": 413,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "RATE_LIMITED": 429,
    "FORBIDDEN": 403,
    "MODEL_NOT_AVAILABLE": 422,
    "PROVIDER_REJECTED": 502,
    "PROVIDER_UNAVAILABLE": 502,
    "PROVIDER_TIMEOUT_UNKNOWN": 504,
    "DELIVERY_FAILED": 502,
    "INTERNAL_ERROR": 500,
}


class AppError(Exception):
    """所有可预期的业务异常都从这里抛出，由全局处理器转成统一响应体。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code = status_code or ERROR_STATUS.get(code, 400)


# --- 便捷构造器 ---------------------------------------------------------


def not_found(message: str = "资源不存在或已删除", **details: Any) -> AppError:
    return AppError("RESOURCE_NOT_FOUND", message, details=details or None)


def invalid(message: str = "请求参数不合法", **details: Any) -> AppError:
    return AppError("INVALID_REQUEST", message, details=details or None)


def conflict(code: str, message: str, **details: Any) -> AppError:
    return AppError(code, message, details=details or None)


def forbidden(message: str = "当前角色无权执行该操作") -> AppError:
    return AppError("FORBIDDEN", message)


def business(message: str, **details: Any) -> AppError:
    return AppError("BUSINESS_RULE_VIOLATION", message, details=details or None)
