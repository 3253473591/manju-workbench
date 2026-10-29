"""口令哈希与会话令牌（`02` §1 / §5.1）。

口令 `argon2id`；会话是 7 天有效、无 refresh 的 HS256 JWT，放 HttpOnly Cookie。
`sub` 只放用户 id，别放角色 —— 角色每次请求从库里读，改权限不需要用户重新登录。
"""

from __future__ import annotations

import time

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from manju.config import Settings
from manju.errors import ApiError, ErrorCode

_hasher = PasswordHasher()

PASSWORD_MIN = 10
PASSWORD_MAX = 72
ALGORITHM = "HS256"


def password_problem(password: str) -> str | None:
    """按 `03` §6.1 的口令规则返回问题描述，合格返回 None。"""
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return f"口令长度需在 {PASSWORD_MIN}–{PASSWORD_MAX} 之间"
    if password.isdigit():
        return "口令不能是纯数字"
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (Argon2Error, ValueError):
        # VerifyMismatchError（口令不对）、InvalidHash（库里那条哈希不是合法 argon2 串，
        # 例如 seed 用户写的不可用占位值）都按"验证失败"处理 —— 对调用方都是 401，
        # 不该变成 500。argon2-cffi 21.x 的 InvalidHash 继承 ValueError 而非 Argon2Error。
        return False


def issue_token(user_id: int, settings: Settings) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + settings.jwt_ttl},
        settings.jwt_secret,
        algorithm=ALGORITHM,
    )


def read_token(token: str | None, settings: Settings) -> int:
    if not token:
        raise ApiError(ErrorCode.AUTH_REQUIRED)
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM],
                             options={"require": ["sub", "iat", "exp"]})
        user_id = int(payload["sub"])
        if not 1 <= user_id <= 2**64 - 1:
            raise ValueError("invalid subject")
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise ApiError(ErrorCode.AUTH_REQUIRED, {"reason": type(exc).__name__}) from exc
    return user_id
