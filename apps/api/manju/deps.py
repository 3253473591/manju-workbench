"""FastAPI 依赖与权限判定。

`02` §2.1 的判定顺序是**唯一算法**，所以权限只在这里实现一次：
`can_write_project()` 与 `can_write_global()` 是两个函数，不得合并成一个 `is_admin` ——
admin 对所有项目只读（不能跑生成、不能改提示词），只对 Skill 与 provider 有写权限。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Path, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from manju.config import COOKIE_NAME, Settings
from manju.constants import PAGE_LIMIT_DEFAULT, PAGE_LIMIT_MAX
from manju.errors import ApiError, ErrorCode
from manju.models import Project, ProjectMember, User
from manju.security import read_token


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.sessionmaker()
    try:
        yield session
    finally:
        session.close()


def current_user(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    user_id = read_token(request.cookies.get(COOKIE_NAME), settings)
    user = session.get(User, user_id)
    if user is None:
        raise ApiError(ErrorCode.AUTH_REQUIRED)
    if not user.is_active:
        raise ApiError(ErrorCode.AUTH_DISABLED)
    return user


def can_write_global(user: User) -> bool:
    return user.role == "admin"


def can_write_any_project(session: Session, user: User) -> bool:
    """`/auth/me` 的 capabilities 用真实数据算，不写死 —— 前端只依赖它做显隐（`02` §5.2）。"""
    if user.role == "admin":
        return False
    if session.scalar(select(func.count()).select_from(Project).where(Project.owner_id == user.id)):
        return True
    owned_or_member = session.scalar(
        select(func.count())
        .select_from(ProjectMember)
        .where(ProjectMember.user_id == user.id, ProjectMember.role == "member")
    )
    return bool(owned_or_member)


def can_write_project(session: Session, user: User, project: Project) -> bool:
    """读：任何登录用户；写：owner 或 member；admin 始终只读内容。"""
    if user.role == "admin":
        return False
    if project.owner_id == user.id:
        return True
    membership = session.get(ProjectMember, (project.id, user.id))
    return membership is not None and membership.role == "member"


def project_for_read(session: Session, user: User, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    return project


def project_for_write(session: Session, user: User, project_id: int) -> Project:
    """资源归属一律从 id 反查 project 再判权限（`02` §5.3 第 1 步），不先查资源。"""
    project = project_for_read(session, user, project_id)
    if project.status == "archived":
        raise ApiError(ErrorCode.PERM_READONLY_PROJECT, {"reason": "archived"})
    if not can_write_project(session, user, project):
        raise ApiError(ErrorCode.PERM_READONLY_PROJECT)
    return project


def require_global_owner(user: User) -> User:
    if not can_write_global(user):
        raise ApiError(ErrorCode.PERM_ADMIN_ONLY)
    return user


def parse_if_match(raw: str | None) -> int:
    """`If-Match: "7"` → 7。缺失、未加引号、非数字一律 422（`03` §13）。"""
    if raw is None:
        raise ApiError(ErrorCode.VALIDATION_SHAPE, {"header": "If-Match", "reason": "required"})
    value = raw.strip()
    if len(value) < 3 or not (value.startswith('"') and value.endswith('"')):
        raise ApiError(ErrorCode.VALIDATION_SHAPE, {"header": "If-Match", "reason": "must be quoted"})
    try:
        return int(value[1:-1])
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_SHAPE, {"header": "If-Match", "reason": "not a number"}) from exc


def check_version(expected: int, actual: int) -> None:
    if expected != actual:
        raise ApiError(ErrorCode.STALE_VERSION, {"expected": expected, "current": actual})


@dataclass(frozen=True)
class Paging:
    limit: int
    cursor: int | None


# 路径 id：非法形态（/clips/abc、/clips/-1、20 位超 BIGINT UNSIGNED）→ 422，
# 合法但不存在 → 404。两者必须分开，否则前端不知道该改 URL 还是提示资源已删（`03` §6.7）。
PathId = Annotated[int, Path(ge=1, le=2**64 - 1)]


def paging(
    limit: int = Query(PAGE_LIMIT_DEFAULT, ge=1),
    cursor: int | None = None,
) -> Paging:
    # 超上限截到上限而不是报错（`03` §6.2：分页是体验，不是业务正确性）
    return Paging(limit=min(limit, PAGE_LIMIT_MAX), cursor=cursor)


def cursor_page(session: Session, stmt, model, page: Paging) -> tuple[list, int | None]:
    """游标分页：id 倒序，`cursor` 是上一页最后一条的 id。

    `cursor` 非法（<=0）或指向已删记录时返回空页，不报错（`03` §6.2）。
    """
    if page.cursor is not None:
        if page.cursor < 1:
            return [], None
        stmt = stmt.where(model.id < page.cursor)
    rows = list(session.scalars(stmt.order_by(model.id.desc()).limit(page.limit + 1)))
    items = rows[: page.limit]
    return items, (items[-1].id if len(rows) > page.limit else None)
