"""Clip 与内部镜头、参考资产引用的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel


class ClipShotInput(ApiModel):
    shotNo: int | None = Field(default=None, ge=1)
    startMs: int | None = Field(default=None, ge=0)
    endMs: int | None = Field(default=None, ge=0)
    visualText: str | None = None
    cameraText: str | None = None
    dialogueText: str | None = None
    soundText: str | None = None


class ClipShotOut(ApiModel):
    id: str
    clipId: str
    shotNo: int
    startMs: int
    endMs: int
    visualText: str = ""
    cameraText: str = ""
    dialogueText: str = ""
    soundText: str = ""


class ClipShotEnvelope(ApiModel):
    shot: ClipShotOut


class ClipInput(ApiModel):
    clipNo: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=200)
    durationMs: int | None = Field(default=None, ge=0)
    characters: list[str] | None = None
    locationName: str | None = Field(default=None, max_length=200)
    body: str | None = None
    status: str | None = None
    expectedVersion: int | None = Field(default=None, ge=1)


class ClipOut(ApiModel):
    id: str
    episodeId: str
    documentId: str | None = None
    sourceRevisionId: str | None = None
    clipNo: int
    title: str
    durationMs: int
    characters: list[str] = Field(default_factory=list)
    locationName: str = ""
    status: str
    body: str = ""
    version: int
    createdAt: str
    updatedAt: str


class ClipEnvelope(ApiModel):
    clip: ClipOut


class ClipDetail(ApiModel):
    clip: ClipOut
    shots: list[ClipShotOut] = Field(default_factory=list)
    references: list[dict] = Field(default_factory=list)


class ClipList(ApiModel):
    items: list[ClipOut] = Field(default_factory=list)
    total: int = 0


class ReorderClipRequest(ApiModel):
    clipNo: int = Field(ge=1)


class AddReferenceRequest(ApiModel):
    assetId: str
    slotNo: int | None = Field(default=None, ge=0)
    required: bool = True


class VideoPlanRequest(ApiModel):
    requirement: str = Field(default="", max_length=4000)
    modelConfigId: str | None = None
    productLabel: str | None = None
