"""统一的请求/响应基础模型。"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiModel(BaseModel):
    """所有响应模型的基类：camelCase 输出，允许从 ORM 对象构造。"""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        alias_generator=None,
    )


class Page(ApiModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    pageSize: int = 20


class ListResult(ApiModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    total: int = 0


class ErrorBody(ApiModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    success: bool = False
    error: ErrorBody
    requestId: str = ""
    timestamp: str = ""
