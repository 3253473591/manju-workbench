"""项目与剧集路由。

读取类接口对三种角色都开放（manager 是只读监督角色，必须能看到项目和产出）；
写入类接口只允许 user / admin。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, DbSession, EditorUser
from app.schemas.project import (
    CreateProjectRequest,
    EpisodeEnvelope,
    EpisodeList,
    ProjectDetail,
    ProjectPage,
    UpdateProjectRequest,
)
from app.services import project_service

router = APIRouter(tags=["projects"])

PageQuery = Annotated[int, Query(ge=1)]
PageSizeQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("/projects", response_model=ProjectPage, summary="项目列表")
def list_projects(
    user: CurrentUser,
    db: DbSession,
    page: PageQuery = 1,
    pageSize: PageSizeQuery = 20,  # noqa: N803 - 契约里的名字
) -> dict:
    return project_service.list_projects(db, page=page, page_size=pageSize)


@router.post(
    "/projects",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectDetail,
    summary="新建项目（自动创建第 1 集与大纲节点）",
)
def create_project(payload: CreateProjectRequest, user: EditorUser, db: DbSession) -> dict:
    return project_service.create_project(
        db, name=payload.name, description=payload.description
    )


@router.get("/projects/{projectId}", response_model=ProjectDetail, summary="项目详情")
def get_project(projectId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    return project_service.get_project_detail(db, projectId)


@router.patch("/projects/{projectId}", response_model=ProjectDetail, summary="修改项目")
def update_project(
    projectId: str,  # noqa: N803
    payload: UpdateProjectRequest,
    user: EditorUser,
    db: DbSession,
) -> dict:
    return project_service.update_project(
        db, projectId, payload.model_dump(exclude_unset=True)
    )


@router.delete(
    "/projects/{projectId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="软删除项目",
)
def delete_project(projectId: str, user: EditorUser, db: DbSession) -> None:  # noqa: N803
    project_service.delete_project(db, projectId)


@router.get(
    "/projects/{projectId}/episodes",
    response_model=EpisodeList,
    summary="项目下的剧集",
)
def list_episodes(projectId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    items = project_service.list_episodes(db, projectId)
    return {"items": items, "total": len(items)}


@router.get("/episodes/{episodeId}", response_model=EpisodeEnvelope, summary="剧集详情")
def get_episode(episodeId: str, user: CurrentUser, db: DbSession) -> dict:  # noqa: N803
    episode = project_service.get_episode_or_404(db, episodeId)
    return {"episode": project_service.episode_out(episode)}
