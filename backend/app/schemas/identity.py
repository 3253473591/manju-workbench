"""Identity 的请求与响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel, Page


class RegisterRequest(ApiModel):
    username: str = Field(min_length=3, max_length=120)
    # 长度策略由 auth_service 判定，返回契约里的 PASSWORD_POLICY_FAILED，
    # 不在这里用 min_length 变成传输层的 INVALID_REQUEST。
    password: str = Field(max_length=128)
    displayName: str = Field(min_length=1, max_length=120)


class LoginRequest(ApiModel):
    username: str
    password: str


class UserOut(ApiModel):
    id: str
    username: str
    displayName: str
    role: str
    status: str
    lastLoginAt: str | None = None
    createdAt: str


class UserEnvelope(ApiModel):
    user: UserOut


class LoginResult(ApiModel):
    user: UserOut
    sessionToken: str
    expiresAt: str


class UserPage(Page[UserOut]):
    pass


class UpdateUserRequest(ApiModel):
    role: str | None = None
    status: str | None = None
    displayName: str | None = Field(default=None, min_length=1, max_length=120)
