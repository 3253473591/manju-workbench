"""Project 领域：项目与剧集。首版每个项目自动创建「第 1 集」。"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Field

from app.core.utils import new_ulid
from app.models.common import SoftDeleteMixin, TimestampMixin, enum_field
from app.models.enums import EpisodeStatus, ProjectStatus


class Project(SoftDeleteMixin, TimestampMixin, table=True):
    __tablename__ = "projects"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    name: str = Field(max_length=200, nullable=False)
    description: str = Field(default="", max_length=2000)
    status: ProjectStatus = enum_field(ProjectStatus, ProjectStatus.active)
    cover_media_id: Optional[str] = Field(default=None, max_length=26)


class Episode(SoftDeleteMixin, TimestampMixin, table=True):
    __tablename__ = "episodes"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    project_id: str = Field(foreign_key="projects.id", index=True, max_length=26)
    number: int = Field(default=1, nullable=False)
    title: str = Field(default="", max_length=200)
    status: EpisodeStatus = enum_field(EpisodeStatus, EpisodeStatus.active)
