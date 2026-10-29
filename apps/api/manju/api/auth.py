"""认证。会话是 HttpOnly Cookie 里的 JWT，7 天有效、无 refresh（`02` §1）。"""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import UTC, datetime
from threading import Lock

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from manju.config import COOKIE_NAME, Settings
from manju.constants import LOGIN_FAIL_LIMIT, LOGIN_FAIL_WINDOW
from manju.deps import can_write_any_project, can_write_global, current_user, get_session, get_settings
from manju.errors import ApiError, ErrorCode
from manju.models import User
from manju.schemas import Capabilities, LoginIn, MeOut, RegisterIn, UserOut
from manju.security import hash_password, issue_token, password_problem, verify_password

router = APIRouter(tags=["auth"])

# 登录失败计数是 `03` §12.4 明确允许的进程内状态：重启清零，不参与业务判定
_FAILURES: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_FAILURES_LOCK = Lock()
_FAILURE_KEYS_MAX = 4096


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


def _guard_login_attempts(ip: str, email: str) -> None:
    now = time.monotonic()
    with _FAILURES_LOCK:
        while _FAILURES and now - next(iter(_FAILURES.values()))[-1] >= LOGIN_FAIL_WINDOW:
            _FAILURES.popitem(last=False)
        recent = [stamp for stamp in _FAILURES.get((ip, email), []) if now - stamp < LOGIN_FAIL_WINDOW]
    if len(recent) >= LOGIN_FAIL_LIMIT:
        retry_after = int(LOGIN_FAIL_WINDOW - (now - recent[0])) + 1
        raise ApiError(
            ErrorCode.RATE_LIMITED,
            {"retry_after_s": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


def _note_failure(ip: str, email: str) -> None:
    with _FAILURES_LOCK:
        key = (ip, email)
        _FAILURES[key] = (_FAILURES.get(key, []) + [time.monotonic()])[-LOGIN_FAIL_LIMIT:]
        _FAILURES.move_to_end(key)
        if len(_FAILURES) > _FAILURE_KEYS_MAX:
            _FAILURES.popitem(last=False)


def _set_session(response: Response, user: User, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issue_token(user.id, settings),
        max_age=settings.jwt_ttl,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _capabilities(session: Session, user: User) -> Capabilities:
    return Capabilities(
        can_create_project=user.role == "lead",
        can_write_global=can_write_global(user),
        can_write_any_project=can_write_any_project(session, user),
    )


@router.post("/auth/register", status_code=201)
def register(
    payload: RegisterIn,
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    problem = password_problem(payload.password)
    if problem:
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"field": "password", "reason": problem})
    email = payload.email.strip().lower()
    if session.scalar(select(User.id).where(User.email == email)) is not None:
        raise ApiError(ErrorCode.CONFLICT_EMAIL)
    user = User(
        email=email,
        pwd_hash=hash_password(payload.password),
        role="user",  # 注册不接受客户端传角色（02 §5.1）
        is_active=1,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    try:
        with session.begin_nested():
            session.add(user)
            session.flush()
    except IntegrityError as exc:
        if exc.orig.args[0] == 1062:
            raise ApiError(ErrorCode.CONFLICT_EMAIL) from exc
        raise
    session.commit()
    _set_session(response, user, settings)
    return UserOut.model_validate(user)


@router.post("/auth/login")
def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    email = payload.email.strip().lower()
    ip = _client_ip(request)
    _guard_login_attempts(ip, email)
    user = session.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(user.pwd_hash, payload.password):
        _note_failure(ip, email)
        raise ApiError(ErrorCode.AUTH_INVALID)
    if not user.is_active:
        raise ApiError(ErrorCode.AUTH_DISABLED)
    with _FAILURES_LOCK:
        _FAILURES.pop((ip, email), None)
    _set_session(response, user, settings)
    return UserOut.model_validate(user)


@router.post("/auth/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/auth/me")
def me(user: User = Depends(current_user), session: Session = Depends(get_session)) -> MeOut:
    return MeOut(user=UserOut.model_validate(user), capabilities=_capabilities(session, user))
