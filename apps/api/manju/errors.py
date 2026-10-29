"""错误码总表与统一错误封装（`02` §4 / `00` §3.7）。

这里是错误码的**唯一代码来源**：新增码同时改本文件和 `02` §4，两边的 HTTP 状态、
用户文案必须一致（`00` §5 第 3 条）。`SPECS` 的文案逐字取自 `02` §4 的表格。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fastapi import Request
from fastapi.responses import JSONResponse


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    AUTH_DISABLED = "AUTH_DISABLED"
    PERM_DENIED = "PERM_DENIED"
    PERM_ADMIN_ONLY = "PERM_ADMIN_ONLY"
    PERM_READONLY_PROJECT = "PERM_READONLY_PROJECT"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    UNSUPPORTED_MEDIA = "UNSUPPORTED_MEDIA"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    STALE_VERSION = "STALE_VERSION"
    CONFLICT_CLIP_INDEX = "CONFLICT_CLIP_INDEX"
    CONFLICT_SKILL_ACTIVE = "CONFLICT_SKILL_ACTIVE"
    CONFLICT_ASSET_REFERENCED = "CONFLICT_ASSET_REFERENCED"
    CONFLICT_HAS_REVISIONS = "CONFLICT_HAS_REVISIONS"
    PUBLISH_BLOCKED = "PUBLISH_BLOCKED"
    CONFLICT_EMAIL = "CONFLICT_EMAIL"
    LOCK_HELD = "LOCK_HELD"
    IDEM_KEY_REUSED = "IDEM_KEY_REUSED"
    VALIDATION_SHAPE = "VALIDATION_SHAPE"
    VALIDATION_RANGE = "VALIDATION_RANGE"
    VALIDATION_INVARIANT = "VALIDATION_INVARIANT"
    MODEL_REF_LIMIT = "MODEL_REF_LIMIT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    PROJECT_BUSY = "PROJECT_BUSY"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    PROVIDER_AUTH = "PROVIDER_AUTH"
    ASSET_FETCH_FAILED = "ASSET_FETCH_FAILED"
    CONTENT_REJECTED = "CONTENT_REJECTED"
    JOB_STATE_INVALID = "JOB_STATE_INVALID"
    JOB_NOT_CANCELLABLE = "JOB_NOT_CANCELLABLE"
    ACCEPTANCE_FAIL = "ACCEPTANCE_FAIL"
    ESCALATED = "ESCALATED"
    SKILL_TOOL_UNKNOWN = "SKILL_TOOL_UNKNOWN"
    SKILL_NOT_DRYRUN = "SKILL_NOT_DRYRUN"
    INTERNAL = "INTERNAL"
    INTERNAL_STATE = "INTERNAL_STATE"


@dataclass(frozen=True)
class ErrorSpec:
    status: int | None  # None = 只作为 job.error_code 的异步终态，不出现在 HTTP 响应里
    message: str
    layer: str
    retryable: bool = False


SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.AUTH_REQUIRED: ErrorSpec(401, "请先登录", "api"),
    ErrorCode.AUTH_INVALID: ErrorSpec(401, "邮箱或密码错误", "api"),
    ErrorCode.AUTH_DISABLED: ErrorSpec(403, "账号已停用", "api"),
    ErrorCode.PERM_DENIED: ErrorSpec(403, "无权执行该操作", "api"),
    ErrorCode.PERM_ADMIN_ONLY: ErrorSpec(403, "仅超管可操作", "api"),
    ErrorCode.PERM_READONLY_PROJECT: ErrorSpec(403, "该项目对你只读", "api"),
    ErrorCode.NOT_FOUND: ErrorSpec(404, "资源不存在", "api"),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(405, "不支持的请求方法", "api"),
    ErrorCode.UNSUPPORTED_MEDIA: ErrorSpec(415, "不支持的文件类型", "api"),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorSpec(413, "内容超出大小上限", "api"),
    ErrorCode.STALE_VERSION: ErrorSpec(412, "内容已被他人修改，请刷新后重试", "api"),
    ErrorCode.CONFLICT_CLIP_INDEX: ErrorSpec(409, "该序号已被占用", "service"),
    ErrorCode.CONFLICT_SKILL_ACTIVE: ErrorSpec(409, "该技能已有生效版本", "service"),
    # 资产不级联删：被引用就拒绝，比"删完让槽位悬空"更安全（`01` §4.5）
    ErrorCode.CONFLICT_ASSET_REFERENCED: ErrorSpec(409, "该资产正被引用，不能删除", "service"),
    # revision 走不掉审计：删掉它的载体 clip/集 = 把已付的钱从台账里带走（`03` §3）
    ErrorCode.CONFLICT_HAS_REVISIONS: ErrorSpec(409, "该内容已有付费产物，不能删除", "service"),
    # 发布门：四道检查（产物/审批/质检/预算）任一不过就拒，detail 带逐项结果
    ErrorCode.PUBLISH_BLOCKED: ErrorSpec(409, "发布条件不满足", "service"),
    ErrorCode.CONFLICT_EMAIL: ErrorSpec(409, "该邮箱已注册", "api"),
    ErrorCode.LOCK_HELD: ErrorSpec(409, "该片段正在被他人编辑", "service"),
    ErrorCode.IDEM_KEY_REUSED: ErrorSpec(409, "幂等键已用于不同请求", "api"),
    ErrorCode.VALIDATION_SHAPE: ErrorSpec(422, "请求格式不正确", "api"),
    ErrorCode.VALIDATION_RANGE: ErrorSpec(422, "参数超出允许范围", "api"),
    ErrorCode.VALIDATION_INVARIANT: ErrorSpec(422, "分镜/参考图/台词不自洽", "domain"),
    ErrorCode.MODEL_REF_LIMIT: ErrorSpec(422, "该模型最多支持 N 张图 / M 段视频，当前超出", "domain"),
    ErrorCode.BUDGET_EXCEEDED: ErrorSpec(402, "本次生成预算已用尽", "service"),
    ErrorCode.PROJECT_BUSY: ErrorSpec(429, "该项目已有生成任务在跑", "service", retryable=True),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, "操作过于频繁，请稍后再试", "api", retryable=True),
    ErrorCode.PROVIDER_RATE_LIMITED: ErrorSpec(429, "上游限流，稍后自动重试", "providers", retryable=True),
    ErrorCode.PROVIDER_TIMEOUT: ErrorSpec(504, "上游超时", "providers", retryable=True),
    ErrorCode.PROVIDER_UNAVAILABLE: ErrorSpec(503, "上游暂不可用", "providers", retryable=True),
    # 不是"上游挂了"，是"管理员手动停用了这条通道" —— 语义必须分开，否则排障会跑偏
    ErrorCode.PROVIDER_DISABLED: ErrorSpec(503, "该通道已被管理员停用", "providers"),
    ErrorCode.PROVIDER_AUTH: ErrorSpec(502, "上游鉴权失败，请联系管理员检查 Key", "providers"),
    ErrorCode.ASSET_FETCH_FAILED: ErrorSpec(502, "素材已生成，但下载失败，可重试下载", "providers"),
    ErrorCode.CONTENT_REJECTED: ErrorSpec(422, "内容审核未通过，已回退提示词", "providers"),
    ErrorCode.JOB_STATE_INVALID: ErrorSpec(409, "当前状态不允许该操作", "jobs"),
    ErrorCode.JOB_NOT_CANCELLABLE: ErrorSpec(409, "任务已结束，无法取消", "jobs"),
    ErrorCode.ACCEPTANCE_FAIL: ErrorSpec(None, "验收点连续 3 次未通过", "agent"),
    ErrorCode.ESCALATED: ErrorSpec(None, "无法定位失败原因，需人工介入", "agent"),
    ErrorCode.SKILL_TOOL_UNKNOWN: ErrorSpec(422, "Skill 引用了未注册的工具", "domain"),
    ErrorCode.SKILL_NOT_DRYRUN: ErrorSpec(409, "该版本尚未通过试跑，不能发布", "domain"),
    ErrorCode.INTERNAL: ErrorSpec(500, "服务内部错误，请附带 request_id 反馈", "任意"),
    ErrorCode.INTERNAL_STATE: ErrorSpec(500, "状态异常（说明是 bug）", "任意"),
}


class ApiError(Exception):
    """业务异常。`detail` 只放稳定字段（`02` §9.4）；上游原文不进 `message`。"""

    def __init__(self, code: ErrorCode, detail: dict | None = None, headers: dict[str, str] | None = None):
        self.code = code
        self.detail = detail or {}
        self.headers = headers or {}
        super().__init__(code.value)


def error_response(request: Request, error: ApiError) -> JSONResponse:
    spec = SPECS[error.code]
    body = {
        "error": {
            "code": error.code.value,
            "message": spec.message,
            "detail": error.detail,
            "request_id": getattr(request.state, "request_id", None),
        }
    }
    headers = dict(error.headers)
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        headers.setdefault("X-Request-Id", request_id)
    return JSONResponse(status_code=spec.status or 500, content=body, headers=headers)
