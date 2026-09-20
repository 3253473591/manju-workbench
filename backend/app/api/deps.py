"""FastAPI 依赖：会话、鉴权、角色校验、请求标识。

权限必须在后端路由和应用服务两层校验；前端隐藏按钮只属于体验优化。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlmodel import Session

from app.core.errors import AppError
from app.core.security import hash_token
from app.db.engine import get_session
from app.models.enums import AccountStatus, Role
from app.models.identity import Session as SessionModel
from app.models.identity import User
from app.services import auth_service

DbSession = Annotated[Session, Depends(get_session)]


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


RequestId = Annotated[str, Depends(get_request_id)]


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def get_current_session(request: Request, db: DbSession) -> SessionModel | None:
    token = _bearer_token(request)
    if not token:
        return None
    record = auth_service.find_active_session(db, hash_token(token))
    return record


def get_current_user(
    db: DbSession, record: Annotated[SessionModel | None, Depends(get_current_session)]
) -> User:
    if record is None:
        raise AppError("AUTH_REQUIRED", "请先登录")
    user = db.get(User, record.user_id)
    if user is None:
        raise AppError("AUTH_INVALID", "会话无效，请重新登录")
    if user.status != AccountStatus.active:
        raise AppError("ACCOUNT_DISABLED", "账号已被停用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: Role) -> Callable[[User], User]:
    """生成一个角色校验依赖；越权返回 403。"""

    allowed = set(roles)

    def dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise AppError("FORBIDDEN", "当前角色无权执行该操作")
        return user

    return dependency


#: 可编辑生产内容与提交生成的角色（PRD §3）
EditorUser = Annotated[User, Depends(require_roles(Role.user, Role.admin))]
#: 可读取费用与项目合计的角色
BillingViewer = Annotated[User, Depends(require_roles(Role.manager, Role.admin))]
#: 仅管理员
AdminUser = Annotated[User, Depends(require_roles(Role.admin))]


def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """生成类写请求必须携带。缺失直接 400，避免幂等账本形同虚设。"""
    if not idempotency_key or len(idempotency_key.strip()) < 8:
        raise AppError(
            "INVALID_REQUEST",
            "生成类请求必须携带 Idempotency-Key（至少 8 个字符）",
            details={"header": "Idempotency-Key"},
        )
    return idempotency_key.strip()


IdempotencyKey = Annotated[str, Depends(get_idempotency_key)]
