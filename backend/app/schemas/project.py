"""Project 与 Episode 的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import ApiModel, Page


class CreateProjectRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class UpdateProjectRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = None


class ProjectOut(ApiModel):
    id: str
    name: str
    description: str
    status: str
    coverMediaId: str | None = None
    createdAt: str
    updatedAt: str


class EpisodeOut(ApiModel):
    id: str
    projectId: str
    number: int
    title: str
    status: str
    createdAt: str


class ProjectDetail(ApiModel):
    project: ProjectOut
    episodes: list[EpisodeOut] = Field(default_factory=list)
    outlineNodeId: str | None = None


class ProjectPage(Page[ProjectOut]):
    pass


class EpisodeEnvelope(ApiModel):
    episode: EpisodeOut


class EpisodeList(ApiModel):
    items: list[EpisodeOut] = Field(default_factory=list)
    total: int = 0
