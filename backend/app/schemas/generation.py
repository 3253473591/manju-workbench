"""生成提交与 attempt 的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel, Page


class SubmissionAccepted(ApiModel):
    submissionId: str
    operation: str
    logicalStatus: str
    requestedCount: int = 1
    nodeIds: list[str] = Field(default_factory=list)


class VideoPlanAccepted(SubmissionAccepted):
    mediaNode: dict


class SubmissionOut(ApiModel):
    id: str
    operation: str
    logicalStatus: str
    requestedCount: int
    episodeId: str | None = None
    nodeId: str | None = None
    targetType: str | None = None
    targetId: str | None = None
    errorCode: str = ""
    errorMessage: str = ""
    createdAt: str
    finishedAt: str | None = None


class AttemptOut(ApiModel):
    id: str
    submissionId: str
    attemptNo: int
    operation: str
    modelConfigId: str | None = None
    contentFingerprint: str = ""
    physicalStatus: str
    deliveryStatus: str
    providerRequestId: str = ""
    providerTaskId: str | None = None
    providerStatusRaw: str = ""
    errorCode: str = ""
    errorMessage: str = ""
    isMock: bool = False
    createdAt: str
    finishedAt: str | None = None
    delivery: dict | None = None
    usage: dict | None = None


class SubmissionDetail(ApiModel):
    submission: SubmissionOut
    attempts: list[AttemptOut] = Field(default_factory=list)
    producedNodeIds: list[str] = Field(default_factory=list)
    result: dict | None = None


class AttemptList(ApiModel):
    items: list[AttemptOut] = Field(default_factory=list)
    total: int = 0


class SubmissionPage(Page[SubmissionOut]):
    pass


class GenerateRequest(ApiModel):
    requirement: str = Field(default="", max_length=4000)
    count: int = Field(default=1, ge=1, le=8)
    seed: int = -1
    modelConfigId: str | None = None
    productLabel: str | None = None
    # 仅预估用：预估要按真实口径算，就得知道是哪种操作、多长、什么分辨率
    operation: str | None = Field(
        default=None,
        description="预估用的操作名；缺省按 text_optimize 处理",
    )
    durationSeconds: int = Field(default=5, ge=1, le=60)
    resolution: str = Field(default="720p", max_length=16)
    imagePixels: int | None = Field(
        default=None, description="预估图片费用时按产物像素分档"
    )
    sourceImages: int = Field(default=0, ge=0, le=9)
    inputTokens: int | None = Field(
        default=None, description="预估文本费用时按已知 token 数量折算"
    )
    outputTokens: int | None = Field(default=None)
