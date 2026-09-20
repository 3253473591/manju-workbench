"""Project 服务：项目、剧集，以及新建项目时自动铺开的第一集与大纲节点。"""

from __future__ import annotations

from sqlmodel import Session, func, select

from app.core.errors import AppError
from app.core.utils import to_iso, utcnow
from app.models.canvas import CanvasNode, CanvasNodeLayout
from app.models.common import touch
from app.models.content import ContentDocument
from app.models.enums import CanvasNodeType, DocumentKind, DocumentStatus, EpisodeStatus, ProjectStatus
from app.models.project import Episode, Project

#: 大纲节点初始位置与尺寸。画布前端用 fitView 把它居中显示。
OUTLINE_NODE_X = 0.0
OUTLINE_NODE_Y = 0.0
OUTLINE_NODE_WIDTH = 420.0
OUTLINE_NODE_HEIGHT = 320.0


def project_out(project: Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "status": project.status.value,
        "coverMediaId": project.cover_media_id,
        "createdAt": to_iso(project.created_at),
        "updatedAt": to_iso(project.updated_at),
    }


def episode_out(episode: Episode) -> dict:
    return {
        "id": episode.id,
        "projectId": episode.project_id,
        "number": episode.number,
        "title": episode.title,
        "status": episode.status.value,
        "createdAt": to_iso(episode.created_at),
    }


def get_project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "项目不存在或已删除")
    return project


def get_episode_or_404(db: Session, episode_id: str) -> Episode:
    episode = db.get(Episode, episode_id)
    if episode is None or episode.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "剧集不存在或已删除")
    return episode


def create_project(db: Session, *, name: str, description: str = "") -> dict:
    """新建项目：同一事务内创建项目、第 1 集、大纲文档与居中的大纲节点。"""
    project = Project(name=name.strip(), description=description or "")
    db.add(project)
    db.flush()

    episode = Episode(
        project_id=project.id,
        number=1,
        title="第 1 集",
        status=EpisodeStatus.active,
    )
    db.add(episode)
    db.flush()

    # 文档与节点互相引用，先建文档再建节点，最后回填 document.node_id
    document = ContentDocument(
        episode_id=episode.id,
        kind=DocumentKind.outline,
        title="大纲",
        body="",
        status=DocumentStatus.draft,
    )
    db.add(document)
    db.flush()

    node = CanvasNode(
        episode_id=episode.id,
        node_type=CanvasNodeType.text,
        document_id=document.id,
        label="大纲",
    )
    db.add(node)
    db.flush()

    document.node_id = node.id
    db.add(document)

    db.add(
        CanvasNodeLayout(
            node_id=node.id,
            x=OUTLINE_NODE_X,
            y=OUTLINE_NODE_Y,
            width=OUTLINE_NODE_WIDTH,
            height=OUTLINE_NODE_HEIGHT,
            z_index=0,
        )
    )
    db.commit()
    db.refresh(project)
    db.refresh(episode)

    return {
        "project": project_out(project),
        "episodes": [episode_out(episode)],
        "outlineNodeId": node.id,
    }


def list_projects(db: Session, *, page: int, page_size: int) -> dict:
    base = select(Project).where(Project.deleted_at.is_(None))
    total = db.exec(select(func.count()).select_from(base.subquery())).one()
    rows = db.exec(
        base.order_by(Project.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "items": [project_out(p) for p in rows],
        "total": int(total or 0),
        "page": page,
        "pageSize": page_size,
    }


def get_project_detail(db: Session, project_id: str) -> dict:
    project = get_project_or_404(db, project_id)
    episodes = list_episodes(db, project_id)
    outline_node_id = None
    if episodes:
        first = db.exec(
            select(CanvasNode)
            .join(ContentDocument, ContentDocument.id == CanvasNode.document_id)
            .where(
                CanvasNode.episode_id == episodes[0]["id"],
                CanvasNode.deleted_at.is_(None),
                ContentDocument.kind == DocumentKind.outline,
            )
        ).first()
        outline_node_id = first.id if first else None
    return {
        "project": project_out(project),
        "episodes": episodes,
        "outlineNodeId": outline_node_id,
    }


def list_episodes(db: Session, project_id: str) -> list[dict]:
    get_project_or_404(db, project_id)
    rows = db.exec(
        select(Episode)
        .where(Episode.project_id == project_id, Episode.deleted_at.is_(None))
        .order_by(Episode.number)
    ).all()
    return [episode_out(e) for e in rows]


def update_project(db: Session, project_id: str, payload: dict) -> dict:
    project = get_project_or_404(db, project_id)
    if payload.get("name") is not None:
        project.name = str(payload["name"]).strip()
    if payload.get("description") is not None:
        project.description = str(payload["description"])
    if payload.get("status") is not None:
        try:
            project.status = ProjectStatus(payload["status"])
        except ValueError as exc:
            raise AppError("INVALID_REQUEST", "项目状态取值非法") from exc
    touch(project)
    db.add(project)
    db.commit()
    db.refresh(project)
    return get_project_detail(db, project.id)


def delete_project(db: Session, project_id: str) -> None:
    project = get_project_or_404(db, project_id)
    now = utcnow()
    project.deleted_at = now
    touch(project)
    db.add(project)

    episodes = db.exec(
        select(Episode).where(Episode.project_id == project_id, Episode.deleted_at.is_(None))
    ).all()
    for episode in episodes:
        episode.deleted_at = now
        touch(episode)
        db.add(episode)
    db.commit()
