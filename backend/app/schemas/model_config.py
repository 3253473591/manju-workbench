"""模型配置的请求/响应模型。凭证只保存环境变量名，任何响应都不返回它。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel


class ModelConfigOut(ApiModel):
    id: str
    provider: str
    capability: str
    productLabel: str
    endpointId: str
    actualModel: str
    enabled: bool
    isDefault: bool
    verifiedAt: str | None = None
    spec: dict = Field(default_factory=dict)
    priceAmount: str
    priceCurrency: str
    priceUnit: str
    concurrencyLimit: int
    note: str = ""


class ModelConfigInput(ApiModel):
    provider: str = "ark"
    capability: str
    productLabel: str = Field(min_length=1, max_length=64)
    endpointId: str = ""
    actualModel: str = ""
    enabled: bool = True
    isDefault: bool = False
    spec: dict = Field(default_factory=dict)
    priceAmount: str = "0"
    priceCurrency: str = "CNY"
    priceUnit: str = ""
    concurrencyLimit: int = Field(default=1, ge=1)
    credentialRef: str = ""
    note: str = ""


class ModelConfigEnvelope(ApiModel):
    modelConfig: ModelConfigOut


class ModelConfigList(ApiModel):
    items: list[ModelConfigOut] = Field(default_factory=list)
    total: int = 0
