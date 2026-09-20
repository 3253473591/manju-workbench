"""资产与引用的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel, Page


class AssetOut(ApiModel):
    id: str
    projectId: str
    assetType: str
    canonicalName: str
    variantName: str
    imageKind: str = ""
    status: str
    mediaNodeId: str | None = None
    createdAt: str


class AssetDetail(ApiModel):
    asset: AssetOut
    candidates: list[dict] = Field(default_factory=list)
    usableResultCount: int = 0


class AssetPage(Page[AssetOut]):
    pass


class UpdateAssetRequest(ApiModel):
    canonicalName: str | None = Field(default=None, min_length=1, max_length=200)
    variantName: str | None = Field(default=None, max_length=200)
    imageKind: str | None = Field(default=None, max_length=32)
    status: str | None = None


class AssetPlanItem(ApiModel):
    assetType: str
    canonicalName: str = Field(min_length=1, max_length=200)
    variantName: str = Field(default="", max_length=200)
    imageKind: str = Field(default="", max_length=32)
    clipIds: list[str] = Field(default_factory=list)
    sourceMediaId: str | None = None
    targetName: str = Field(default="", max_length=200)


class AssetPlanConfirmRequest(ApiModel):
    submissionId: str | None = None
    items: list[AssetPlanItem] = Field(min_length=1)


class AssetPlanConfirmResult(ApiModel):
    assets: list[AssetOut] = Field(default_factory=list)
    createdMediaNodes: list[dict] = Field(default_factory=list)
    reused: list[dict] = Field(default_factory=list)
    references: list[dict] = Field(default_factory=list)
    submissionId: str | None = None
    #: 模型返回但实际不存在的 Clip id（例如 clip_001 这类幻觉值）会被丢弃并在此回报
    droppedClipIds: list[str] = Field(default_factory=list)
