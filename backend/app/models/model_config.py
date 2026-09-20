"""模型配置与费用。密钥只保存引用名（environment 变量名），绝不保存密钥值。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlmodel import Field, UniqueConstraint

from app.core.utils import new_ulid
from app.models.common import TimestampMixin, enum_field
from app.models.enums import BillingStatus, ProviderCapability


class ModelConfig(TimestampMixin, table=True):
    """一条 = 一个可选的模型档位。能力未知的 real 选项不能展示为已可用。"""

    __tablename__ = "model_configs"
    __table_args__ = (
        UniqueConstraint(
            "provider", "capability", "product_label", name="uq_model_config_label"
        ),
    )

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    provider: str = Field(default="ark", max_length=32)
    capability: ProviderCapability = enum_field(
        ProviderCapability, ProviderCapability.text, length=16
    )
    #: 产品标签：doubao / seedream-lite / seedream-pro / seedance-mini ...
    product_label: str = Field(max_length=64, nullable=False)
    #: 接入点 ID（ep-...）
    endpoint_id: str = Field(default="", max_length=128)
    #: 供应商响应里的实际模型标识
    actual_model: str = Field(default="", max_length=128)
    enabled: bool = Field(default=True)
    is_default: bool = Field(default=False)
    #: 只有真实调用成功过才写核验时间；未核验不得当作可用
    verified_at: Optional[datetime] = Field(default=None)
    #: 支持规格、参考图上限、seed 能力等；未知字段一律缺省而不是猜
    spec_json: str = Field(default="{}")
    price_amount: Decimal = Field(default=Decimal("0"), max_digits=18, decimal_places=6)
    price_currency: str = Field(default="CNY", max_length=8)
    #: 计价单位：per_1m_input_token / per_1m_output_token / per_image / per_second ...
    price_unit: str = Field(default="", max_length=64)
    concurrency_limit: int = Field(default=1)
    #: 环境变量名，例如 ARK_DOUBAO_API_KEY；密钥值只从环境读取
    credential_ref: str = Field(default="", max_length=128)
    note: str = Field(default="")


class UsageRecord(TimestampMixin, table=True):
    """按 attempt 汇总用量与费用；user 角色不返回任何计费字段。"""

    __tablename__ = "usage_records"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    attempt_id: str = Field(
        foreign_key="generation_attempts.id", index=True, max_length=26
    )
    input_units: Optional[int] = Field(default=None)
    output_units: Optional[int] = Field(default=None)
    #: 原始用量明细，例如 {"input_images": 1, "generated_images": 1}
    unit_json: str = Field(default="{}")
    estimated_amount: Optional[Decimal] = Field(
        default=None, max_digits=18, decimal_places=6
    )
    actual_amount: Optional[Decimal] = Field(
        default=None, max_digits=18, decimal_places=6
    )
    billing_status: BillingStatus = enum_field(
        BillingStatus, BillingStatus.unknown, length=16
    )
    is_mock: bool = Field(default=False)
    note: str = Field(default="")
