"""鉴权路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import CurrentUser, DbSession, get_current_session
from app.models.identity import Session as SessionModel
from app.schemas.identity import (
    LoginRequest,
    LoginResult,
    RegisterRequest,
    UserEnvelope,
)
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserEnvelope,
    summary="注册（角色固定为 user）",
    openapi_extra={"security": []},
)
def register(payload: RegisterRequest, db: DbSession) -> dict:
    user = auth_service.register(
        db,
        username=payload.username,
        password=payload.password,
        display_name=payload.displayName,
    )
    return {"user": auth_service.user_out(user)}


@router.post(
    "/login", response_model=LoginResult, summary="登录", openapi_extra={"security": []}
)
def login(payload: LoginRequest, request: Request, db: DbSession) -> dict:
    user, token, expires_at = auth_service.login(
        db,
        username=payload.username,
        password=payload.password,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {
        "user": auth_service.user_out(user),
        "sessionToken": token,
        "expiresAt": expires_at,
    }


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="退出登录",
)
def logout(
    db: DbSession,
    record: Annotated[SessionModel | None, Depends(get_current_session)],
) -> None:
    auth_service.logout(db, record)


@router.get("/me", response_model=UserEnvelope, summary="当前用户与角色")
def me(user: CurrentUser) -> dict:
    return {"user": auth_service.user_out(user)}
