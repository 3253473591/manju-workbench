"""导入相关的响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel


class ImportResult(ApiModel):
    nodeId: str
    kind: str
    created: dict = Field(default_factory=dict)
    references: list[dict] = Field(default_factory=list)
    mediaItem: dict | None = None
