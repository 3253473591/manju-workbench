"""媒体方案/结果节点与提示词版本的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel


class MediaItemOut(ApiModel):
    id: str
    mediaType: str
    fileKey: str
    originalName: str = ""
    mimeType: str = ""
    sizeBytes: int = 0
    sha256: str = ""
    width: int | None = None
    height: int | None = None
    durationMs: int | None = None
    decodeStatus: str
    fileUrl: str = ""
    thumbnailUrl: str | None = None


class MediaNodeOut(ApiModel):
    id: str
    episodeId: str
    mediaKind: str
    mediaItemId: str | None = None
    sourceMediaId: str | None = None
    promptVersionId: str | None = None
    clipId: str | None = None
    assetId: str | None = None
    targetName: str = ""
    title: str = ""
    blocked: bool = False
    seed: int = -1
    params: dict = Field(default_factory=dict)


class MediaNodeEnvelope(ApiModel):
    mediaNode: MediaNodeOut


class PromptVersionOut(ApiModel):
    id: str
    nodeId: str
    revisionNo: int
    visibleText: str
    hiddenTemplateVersion: str = ""
    finalInputHash: str = ""
    source: str
    createdAt: str


class PromptVersionEnvelope(ApiModel):
    promptVersion: PromptVersionOut


class PromptVersionList(ApiModel):
    items: list[PromptVersionOut] = Field(default_factory=list)
    total: int = 0


class MediaNodeDetail(ApiModel):
    mediaNode: MediaNodeOut
    visiblePrompt: str | None = None
    promptVersions: list[PromptVersionOut] = Field(default_factory=list)
    media: MediaItemOut | None = None
    sourceMedia: MediaItemOut | None = None
    referenceImages: list[MediaItemOut] = Field(default_factory=list)
    frozenClip: dict | None = None
    sourceSummary: dict = Field(default_factory=dict)


class UpdateMediaNodeRequest(ApiModel):
    blocked: bool | None = None
    targetName: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)


class CreatePromptVersionRequest(ApiModel):
    visibleText: str
    select: bool = True


class DrawRequest(ApiModel):
    count: int = Field(default=1, ge=1, le=8)
    seed: int = -1
    modelConfigId: str | None = None
    productLabel: str | None = None


class CreateAssetImagePlanRequest(ApiModel):
    imageKind: str = Field(default="", max_length=32)
    sourceImageKind: str | None = None
    sourceMediaId: str | None = None
    targetName: str = Field(default="", max_length=200)
