"""Identity 领域：账号与会话。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field

from app.core.utils import new_ulid
from app.models.common import TimestampMixin, enum_field
from app.models.enums import AccountStatus, Role


class User(TimestampMixin, table=True):
    __tablename__ = "users"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    username: str = Field(max_length=120, unique=True, index=True, nullable=False)
    password_hash: str = Field(max_length=255, nullable=False)
    display_name: str = Field(max_length=120, nullable=False)
    role: Role = enum_field(Role, Role.user)
    status: AccountStatus = enum_field(AccountStatus, AccountStatus.active)
    last_login_at: Optional[datetime] = Field(default=None)


class Session(TimestampMixin, table=True):
    """随机 token 的哈希 + 过期/吊销时间；token 明文只在创建响应里出现一次。"""

    __tablename__ = "sessions"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    user_id: str = Field(foreign_key="users.id", index=True, max_length=26, nullable=False)
    token_hash: str = Field(max_length=128, unique=True, index=True, nullable=False)
    expires_at: datetime = Field(nullable=False)
    revoked_at: Optional[datetime] = Field(default=None)
    ip_hash: Optional[str] = Field(default=None, max_length=128)
    user_agent: Optional[str] = Field(default=None, max_length=512)
