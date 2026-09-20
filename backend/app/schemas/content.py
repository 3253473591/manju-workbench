"""内容文档（大纲 / 人物设定 / 剧本）的请求与响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel


class ContentRevisionOut(ApiModel):
    id: str
    documentId: str
    revisionNo: int
    source: str
    title: str
    body: str
    metadata: dict = Field(default_factory=dict)
    createdBy: str | None = None
    createdAt: str


class ContentDocumentOut(ApiModel):
    id: str
    episodeId: str
    kind: str
    parentDocumentId: str | None = None
    title: str
    body: str
    status: str
    version: int
    currentRevisionId: str | None = None
    boundOutlineRevisionId: str | None = None
    boundSettingRevisionId: str | None = None
    sourceRevisionId: str | None = None
    nodeId: str | None = None
    createdAt: str
    updatedAt: str


class ContentDocumentDetail(ApiModel):
    document: ContentDocumentOut
    currentRevision: ContentRevisionOut | None = None
    revisions: list[ContentRevisionOut] = Field(default_factory=list)
    hasUnsavedDraft: bool = False


class ContentRevisionList(ApiModel):
    items: list[ContentRevisionOut] = Field(default_factory=list)
    total: int = 0


class UpdateDraftRequest(ApiModel):
    body: str | None = None
    title: str | None = Field(default=None, max_length=200)
    expectedVersion: int | None = Field(default=None, ge=1)


class CreateRevisionRequest(ApiModel):
    body: str | None = None
    title: str | None = Field(default=None, max_length=200)
    source: str | None = None
    expectedVersion: int | None = Field(default=None, ge=1)
    fromRevisionId: str | None = None


class DocumentActionRequest(ApiModel):
    requirement: str = Field(default="", max_length=4000)
    count: int = Field(default=1, ge=1, le=8)
    modelConfigId: str | None = None


class ClipActionRequest(ApiModel):
    requirement: str = Field(default="", max_length=4000)
    count: int = Field(default=1, ge=1, le=8)
    modelConfigId: str | None = None
