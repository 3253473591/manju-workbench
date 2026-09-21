"""公共字段 Mixin 与列工具。

注意：Mixin 里**不使用** `Field(sa_column=...)`。SQLAlchemy 的 Column 实例不能同时挂在
多张表上，而 `sa_column` 会让多个子类复用同一个 Column 对象。普通字段则由 SQLModel
为每个表各自建列，因此这里只用普通字段。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel

from app.core.utils import utcnow


class TimestampMixin(SQLModel):
    created_at: datetime = Field(default_factory=utcnow, nullable=False, index=True)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


class SoftDeleteMixin(SQLModel):
    deleted_at: Optional[datetime] = Field(default=None, index=True)


class VersionMixin(SQLModel):
    """内容编辑表专用：更新必须带 expected_version，不匹配返回 409。"""

    version: int = Field(default=1, nullable=False)


def _sa_enum(enum_cls: type[Enum], *, length: int) -> SAEnum:
    """按 **value** 落库。

    默认的 SAEnum 存的是成员名，一旦成员名为了避开 Python 保留字而加下划线
    （例如 `import_`），库里就会混入 `import_` 这种噪声。统一按 value 存。
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


def enum_field(enum_cls: type[Enum], default: Enum, *, length: int = 32, index: bool = False):
    """把枚举落成 VARCHAR 而不是数据库原生 ENUM，便于迁移与跨库。

    `index=True` 用于高频过滤列（例如 `generation_submissions.logical_status`）：
    没有索引时按状态过滤会扫全表，在建表锁上会放大间隙锁范围，多 worker 下更容易死锁。
    """
    return Field(
        default=default,
        sa_column=Column(
            _sa_enum(enum_cls, length=length), nullable=False, index=index
        ),
    )


def enum_column(enum_cls: type[Enum], *, length: int = 32, nullable: bool = False):
    return Column(_sa_enum(enum_cls, length=length), nullable=nullable)


def touch(obj: object) -> None:
    """在服务层显式刷新 updated_at（Mixin 里不做 onupdate，避免 sa_column 复用问题）。"""
    setattr(obj, "updated_at", utcnow())
