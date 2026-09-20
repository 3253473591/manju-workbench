"""计费响应模型。金额一律 decimal 字符串，不使用浮点。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel, Page


class BillingSummary(ApiModel):
    currency: str = "CNY"
    attemptCount: int = 0
    actualTotal: str = "0"
    estimatedTotal: str = "0"
    mockTotal: str = "0"
    byOperation: list[dict] = Field(default_factory=list)
    unknownUsageCount: int = 0


class BillingRecordOut(ApiModel):
    attemptId: str
    submissionId: str
    operation: str
    modelConfigId: str | None = None
    inputUnits: int | None = None
    outputUnits: int | None = None
    amount: str | None = None
    currency: str = "CNY"
    billingStatus: str
    isMock: bool = False
    createdAt: str


class BillingRecordPage(Page[BillingRecordOut]):
    pass


class EstimateResult(ApiModel):
    currency: str = "CNY"
    estimatedAmount: str = "0"
    isMock: bool = False
    modelConfigId: str | None = None
    unit: str = ""
    notes: list[str] = Field(default_factory=list)
