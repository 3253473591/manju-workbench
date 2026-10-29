"""项目、成员与成本看板。

权限按 `02` §2.1：读任何登录用户；写 owner 或 member；建项目与成员管理要 owner。
`PATCH` 一律要 `If-Match`，重复提交同样 412（`03` §8.2）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from manju.deps import (
    Paging,
    PathId,
    check_version,
    current_user,
    cursor_page,
    get_session,
    paging,
    parse_if_match,
    project_for_read,
    project_for_write,
)
from manju.constants import TIME_RANGE_MAX_DAYS
from manju.errors import ApiError, ErrorCode
from manju.models import Clip, Episode, Project, ProjectMember, Revision, User
from manju.readiness import budget_status
from manju.schemas import (
    CostItem,
    CostOut,
    CostTotal,
    MemberIn,
    MemberOut,
    Page,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)

router = APIRouter(tags=["projects"])


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _owner_only(project: Project, user: User) -> None:
    if project.owner_id != user.id or user.role == "admin":
        raise ApiError(ErrorCode.PERM_DENIED, {"reason": "owner_only"})


@router.get("/projects")
def list_projects(
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    page: Paging = Depends(paging),
) -> Page[ProjectOut]:
    items, next_cursor = cursor_page(session, select(Project), Project, page)
    return Page(items=[ProjectOut.model_validate(item) for item in items], next_cursor=next_cursor)


@router.post("/projects", status_code=201)
def create_project(
    payload: ProjectCreate,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ProjectOut:
    if user.role != "lead":
        raise ApiError(ErrorCode.PERM_DENIED, {"reason": "lead_required"})
    project = Project(
        title=payload.title,
        owner_id=user.id,
        genre=payload.genre,
        tone=payload.tone,
        logline=payload.logline,
        status="active",
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(project)
    session.commit()
    response.headers["ETag"] = f'"{project.version}"'
    return ProjectOut.model_validate(project)


@router.get("/projects/{project_id}")
def get_project(
    project_id: PathId,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ProjectOut:
    project = project_for_read(session, user, project_id)
    response.headers["ETag"] = f'"{project.version}"'
    return ProjectOut.model_validate(project)


@router.patch("/projects/{project_id}")
def patch_project(
    project_id: PathId,
    payload: ProjectUpdate,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ProjectOut:
    project = project_for_write(session, user, project_id)
    _owner_only(project, user)
    check_version(parse_if_match(request.headers.get("If-Match")), project.version)

    changes = payload.model_dump(exclude_unset=True)
    for field in ("title", "status"):
        if field in changes and changes[field] is None:
            raise ApiError(ErrorCode.VALIDATION_SHAPE, {"field": field, "reason": "not nullable"})
    if changes.get("genre") == "":
        raise ApiError(ErrorCode.VALIDATION_SHAPE, {"field": "genre", "reason": "empty string"})
    for field, value in changes.items():
        setattr(project, field, value)
    project.version += 1
    project.updated_at = _now()
    session.commit()
    response.headers["ETag"] = f'"{project.version}"'
    return ProjectOut.model_validate(project)


@router.post("/projects/{project_id}/archive")
def archive_project(
    project_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ProjectOut:
    project = project_for_read(session, user, project_id)
    _owner_only(project, user)
    if project.status != "archived":  # 已归档 → 200 幂等（03 §8.2）
        project.status = "archived"
        project.version += 1
        project.updated_at = _now()
        session.commit()
    return ProjectOut.model_validate(project)


@router.get("/projects/{project_id}/members")
def list_members(
    project_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[MemberOut]:
    project_for_read(session, user, project_id)
    rows = session.execute(
        select(ProjectMember, User.email).join(User, User.id == ProjectMember.user_id).where(
            ProjectMember.project_id == project_id
        )
    ).all()
    return [MemberOut(user_id=member.user_id, email=email, role=member.role) for member, email in rows]


@router.post("/projects/{project_id}/members", status_code=201)
def add_member(
    project_id: PathId,
    payload: MemberIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> MemberOut:
    project = project_for_write(session, user, project_id)
    _owner_only(project, user)
    email = payload.email.strip().lower()
    target = session.scalar(select(User).where(User.email == email))
    if target is None:
        raise ApiError(ErrorCode.NOT_FOUND, {"email": email})
    if target.id == project.owner_id:
        # owner 不落 project_member（01 §5.2：所有权只有一处真相）
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"reason": "owner_is_not_a_member"})
    member = session.get(ProjectMember, (project_id, target.id))
    if member is None:
        member = ProjectMember(project_id=project_id, user_id=target.id, role=payload.role, created_at=_now())
        session.add(member)
    else:
        member.role = payload.role
    session.commit()
    return MemberOut(user_id=target.id, email=email, role=member.role)


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
def remove_member(
    project_id: PathId,
    user_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    project = project_for_write(session, user, project_id)
    _owner_only(project, user)
    member = session.get(ProjectMember, (project_id, user_id))
    if member is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    session.delete(member)
    session.commit()


@router.get("/projects/{project_id}/cost")
def project_cost(
    project_id: PathId,
    group_by: str = Query("model", pattern="^(model|day|clip|episode)$"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> CostOut:
    """只聚合 `revision.cost`，不重算（`02` §5.7）。

    权限按 `02` §2.2：成本看板是 lead / admin 的，普通用户看不到。
    `group_by` 支持 `model` / `day` / `clip`；`stage` 暂不支持 —— pipeline 阶段还没有落到
    revision 上（等服务层）。`day` 的 key 是 **UTC 日期**，前端自己转本地时区。
    """
    project_for_read(session, user, project_id)
    if user.role not in {"lead", "admin"}:
        raise ApiError(ErrorCode.PERM_DENIED, {"reason": "cost_board_requires_lead_or_admin"})
    clip_ids = select(Clip.id).join(Episode, Episode.id == Clip.episode_id).where(
        Episode.project_id == project_id
    )
    where = [Revision.target_kind == "clip", Revision.target_id.in_(clip_ids)]
    if date_from:
        where.append(Revision.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        where.append(Revision.created_at <= datetime.combine(date_to, time.max))
    if date_from and date_to:
        if date_from > date_to:
            raise ApiError(ErrorCode.VALIDATION_RANGE, {"reason": "from > to"})
        # `03` §6.2：跨度上限 366 天。之前只声明了 TIME_RANGE_MAX_DAYS 却没接线，
        # 客户端可以要一个二十年的区间去打全表扫描
        if (date_to - date_from).days > TIME_RANGE_MAX_DAYS:
            raise ApiError(
                ErrorCode.VALIDATION_RANGE,
                {"reason": "range_too_wide", "max_days": TIME_RANGE_MAX_DAYS},
            )

    if group_by == "model":
        key = Revision.model_used
    elif group_by == "day":
        key = func.date(Revision.created_at)
    elif group_by == "episode":
        # 多集项目最常问的"这一集花了多少"：key 直接给人能读的「第N集」
        key = func.concat("第", Episode.idx, "集")
    else:
        # 按片段聚合：key 用 `clip:{id}`，前端拿 id 回查标题即可
        key = func.concat("clip:", Revision.target_id)
    statement = select(
        key.label("key"),
        func.count().label("calls"),
        func.coalesce(func.sum(Revision.input_tokens), 0),
        func.coalesce(func.sum(Revision.video_seconds), Decimal("0")),
        func.coalesce(func.sum(Revision.cost), Decimal("0")),
    ).where(*where)
    if group_by == "episode":
        statement = statement.join(Clip, Clip.id == Revision.target_id).join(
            Episode, Episode.id == Clip.episode_id
        )
    rows = session.execute(statement.group_by(key).order_by(func.sum(Revision.cost).desc())).all()
    items = [
        CostItem(key=str(row[0]), calls=row[1], tokens=int(row[2]), seconds=Decimal(row[3]), cost=Decimal(row[4]))
        for row in rows
    ]
    budget = budget_status(session, project_id)
    return CostOut(
        group_by=group_by,
        items=items,
        total=CostTotal(
            calls=sum(item.calls for item in items),
            cost=sum((item.cost for item in items), Decimal("0")),
        ),
        budget={
            "spent_today": budget["spent_today"],
            "limit": budget["limit"],
            "ratio": budget["ratio"],
            "tier": budget["tier"],
        },
    )
