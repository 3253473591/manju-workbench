"""Generation 领域：幂等账本、每次实际模型调用、交付核验与追溯关联。

三层状态不能互相覆盖：供应商 succeeded 仍可能对应 delivery failed。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, UniqueConstraint

from app.core.utils import new_ulid
from app.models.common import TimestampMixin, enum_field
from app.models.enums import (
    AttemptTarget,
    DeliveryStatus,
    LogicalStatus,
    Operation,
    PhysicalStatus,
)


class GenerationSubmission(TimestampMixin, table=True):
    """用户一次操作的幂等账本。相同 submission_key 的重复请求复用原任务。"""

    __tablename__ = "generation_submissions"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    submission_key: str = Field(max_length=128, unique=True, index=True, nullable=False)
    operation: Operation = enum_field(Operation, Operation.outline_optimize, length=32)
    logical_status: LogicalStatus = enum_field(
        LogicalStatus, LogicalStatus.pending, length=16
    )
    requested_count: int = Field(default=1)
    episode_id: Optional[str] = Field(default=None, max_length=26, index=True)
    target_type: Optional[str] = Field(default=None, max_length=32)
    target_id: Optional[str] = Field(default=None, max_length=26, index=True)
    node_id: Optional[str] = Field(default=None, max_length=26, index=True)
    created_by: Optional[str] = Field(default=None, max_length=26)
    request_id: str = Field(default="", max_length=64)
    #: 冻结的本次需求与上下文摘要（不含密钥、不含隐藏 A 正文以外的敏感值）
    payload_json: str = Field(default="{}")
    error_code: str = Field(default="", max_length=64)
    error_message: str = Field(default="")
    finished_at: Optional[datetime] = Field(default=None)


class GenerationAttempt(TimestampMixin, table=True):
    """每次实际模型调用一行。轮询同一供应商任务不新增 attempt。"""

    __tablename__ = "generation_attempts"
    __table_args__ = (
        UniqueConstraint("provider_task_id", name="uq_attempt_provider_task"),
    )

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    submission_id: str = Field(
        foreign_key="generation_submissions.id", index=True, max_length=26
    )
    attempt_no: int = Field(default=1)
    operation: Operation = enum_field(Operation, Operation.outline_optimize, length=32)
    model_config_id: Optional[str] = Field(default=None, max_length=26, index=True)
    #: 调用前冻结的正文/方案版本、需求、预设版本、模型规格、参考文件及顺序、hash、依赖、批次序号
    input_snapshot_json: str = Field(default="{}")
    content_fingerprint: str = Field(default="", max_length=128)
    physical_status: PhysicalStatus = enum_field(
        PhysicalStatus, PhysicalStatus.reserved, length=16
    )
    delivery_status: DeliveryStatus = enum_field(
        DeliveryStatus, DeliveryStatus.pending, length=16
    )
    provider_request_id: str = Field(default="", max_length=128)
    #: 可空唯一：避免恢复轮询时重复创建供应商任务
    provider_task_id: Optional[str] = Field(default=None, max_length=191)
    provider_response_meta_json: str = Field(default="{}")
    provider_status_raw: str = Field(default="", max_length=64)
    error_code: str = Field(default="", max_length=64)
    error_message: str = Field(default="")
    is_mock: bool = Field(default=False)
    finished_at: Optional[datetime] = Field(default=None)


class DeliveryRecord(TimestampMixin, table=True):
    """交付层：本地文件落盘与结构核验。HTTP 200 或拿到 URL 都不等于交付完成。"""

    __tablename__ = "delivery_records"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    attempt_id: str = Field(
        foreign_key="generation_attempts.id", index=True, max_length=26
    )
    delivery_status: DeliveryStatus = enum_field(
        DeliveryStatus, DeliveryStatus.pending, length=16
    )
    file_key: str = Field(default="", max_length=500)
    sha256: str = Field(default="", max_length=64)
    decode_status: str = Field(default="pending", max_length=16)
    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)
    duration_ms: Optional[int] = Field(default=None)
    thumbnail_file_key: str = Field(default="", max_length=500)
    verified_at: Optional[datetime] = Field(default=None)
    error_message: str = Field(default="")


class AttemptLink(TimestampMixin, table=True):
    """attempt 与文档/节点/Clip 的多对多关联，保证一个 attempt 不被重复计费。"""

    __tablename__ = "attempt_links"
    __table_args__ = (
        UniqueConstraint("attempt_id", "target_type", "target_id", name="uq_attempt_link"),
    )

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    attempt_id: str = Field(
        foreign_key="generation_attempts.id", index=True, max_length=26
    )
    target_type: AttemptTarget = enum_field(
        AttemptTarget, AttemptTarget.canvas_node, length=20
    )
    target_id: str = Field(max_length=26, index=True)
