"""版本链、两版 diff、回滚、成本口径的经验库。

回滚的语义（`03` §8.2）：把 `clip.current_revision_id` 指回某一次**已有**的 take，并把
prompt/negative 恢复成那次实际用的；**不写新 revision**（回滚不花钱，也不产生新产物）。
审批绑定在具体 take 上，所以回到旧 take 必须重新进 `review` 等人看。
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import clip_rules
from manju.api.episodes import _clip_for_write, _clip_views
from manju.deps import (
    Paging,
    PathId,
    current_user,
    cursor_page,
    get_session,
    paging,
    project_for_read,
)
from manju.errors import ApiError, ErrorCode
from manju.models import Clip, Episode, Lesson, Revision, User
from manju.schemas import ClipOut, ClipRollbackIn, LessonOut, Page, RevisionDiff, RevisionOut

router = APIRouter(tags=["revisions"])


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _revision_out(revision: Revision, seq: int | None = None) -> RevisionOut:
    return RevisionOut(
        id=revision.id,
        seq=seq,
        target_kind=revision.target_kind,
        target_id=revision.target_id,
        prompt=revision.prompt,
        negative=revision.negative,
        params=revision.params_json,
        provider=revision.provider,
        model_requested=revision.model_requested,
        model_used=revision.model_used,
        skill_versions=revision.skill_versions_json,
        input_tokens=revision.input_tokens,
        output_tokens=revision.output_tokens,
        video_seconds=revision.video_seconds,
        unit_price=revision.unit_price,
        cost=revision.cost,
        latency_ms=revision.latency_ms,
        motion=revision.motion_json,
        acceptance_result=revision.acceptance_result_json,
        parent_revision_id=revision.parent_revision_id,
        output_asset_id=revision.output_asset_id,
        created_at=revision.created_at,
    )


def _revision_or_404(session: Session, revision_id: int) -> Revision:
    revision = session.get(Revision, revision_id)
    if revision is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    return revision


def _seq_map(session: Session, revisions: list[Revision]) -> dict[int, int]:
    """`revision.id → 同一 target 内的第几版`。`revision` 表没有 version 列（只增不改），
    序号只能按 id 顺序推，所以在这里算成派生字段给前端，避免它自己数（`02` §3.6）。
    """
    if not revisions:
        return {}
    pairs = {(item.target_kind, item.target_id) for item in revisions}
    seq: dict[int, int] = {}
    for kind, target_id in pairs:
        ids = session.scalars(
            select(Revision.id)
            .where(Revision.target_kind == kind, Revision.target_id == target_id)
            .order_by(Revision.id)
        )
        for position, revision_id in enumerate(ids, start=1):
            seq[revision_id] = position
    return seq


@router.get("/clips/{clip_id}/revisions")
def list_revisions(
    clip_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    page: Paging = Depends(paging),
) -> Page[RevisionOut]:
    clip = session.get(Clip, clip_id)
    if clip is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    episode = session.get(Episode, clip.episode_id)
    project_for_read(session, user, episode.project_id)
    stmt = select(Revision).where(Revision.target_kind == "clip", Revision.target_id == clip_id)
    items, next_cursor = cursor_page(session, stmt, Revision, page)
    seq = _seq_map(session, items)
    return Page(items=[_revision_out(item, seq.get(item.id)) for item in items], next_cursor=next_cursor)


@router.get("/revisions/{revision_id}")
def get_revision(
    revision_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> RevisionOut:
    revision = _revision_or_404(session, revision_id)
    if revision.target_kind == "clip":
        clip = session.get(Clip, revision.target_id)
        if clip is not None:
            episode = session.get(Episode, clip.episode_id)
            project_for_read(session, user, episode.project_id)
    return _revision_out(revision, _seq_map(session, [revision]).get(revision.id))


@router.get("/revisions/{left_id}/diff/{right_id}")
def diff_revisions(
    left_id: PathId,
    right_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> RevisionDiff:
    left = _revision_or_404(session, left_id)
    right = _revision_or_404(session, right_id)
    prompt_diff = "".join(
        difflib.unified_diff(
            (left.prompt or "").splitlines(keepends=True),
            (right.prompt or "").splitlines(keepends=True),
            fromfile=f"revision-{left.id}",
            tofile=f"revision-{right.id}",
        )
    )
    left_params = left.params_json or {}
    right_params = right.params_json or {}
    params_diff = {
        key: [left_params.get(key), right_params.get(key)]
        for key in sorted(set(left_params) | set(right_params))
        if left_params.get(key) != right_params.get(key)
    }
    left_skills = left.skill_versions_json or {}
    right_skills = right.skill_versions_json or {}
    skills_diff = {
        key: [left_skills.get(key), right_skills.get(key)]
        for key in sorted(set(left_skills) | set(right_skills))
        if left_skills.get(key) != right_skills.get(key)
    }
    return RevisionDiff(
        a=left.id,
        b=right.id,
        prompt_diff=prompt_diff,
        params_diff=params_diff,
        cost_delta=Decimal(right.cost) - Decimal(left.cost),
        skill_versions_diff=skills_diff,
    )


@router.post("/clips/{clip_id}/rollback")
def rollback_clip(
    clip_id: PathId,
    payload: ClipRollbackIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ClipOut:
    """把片段指回某一次已有产物。幂等：已经是这一版就 200 原样返回（不推进 `version`）。"""
    clip = _clip_for_write(session, user, clip_id)
    revision = session.get(Revision, payload.revision_id)
    if revision is None or revision.target_kind != "clip" or revision.target_id != clip.id:
        raise ApiError(ErrorCode.NOT_FOUND, {"revision_id": payload.revision_id})
    if clip.current_revision_id == revision.id:
        return _clip_views(session, [clip])[0]
    clip.current_revision_id = revision.id
    clip.prompt = revision.prompt
    clip.negative = revision.negative
    if revision.output_asset_id is not None:
        # 换回某一次**产物**等于换了画面 → 审批绑定在具体 take 上，必须回炉重审（`02` §6.2）。
        # 而文本 step 的 revision 没有产物（S4 只写提示词），回滚它只是恢复提示词，不动状态。
        clip_rules.enforce_transition(clip.status, "review")
        clip.status = "review"
    clip.version += 1
    clip.updated_at = _now()
    session.commit()
    return _clip_views(session, [clip])[0]


@router.get("/projects/{project_id}/lessons")
def list_lessons(
    project_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    page: Paging = Depends(paging),
) -> Page[LessonOut]:
    project_for_read(session, user, project_id)
    stmt = select(Lesson).where(Lesson.scope == f"project:{project_id}", Lesson.archived_at.is_(None))
    items, next_cursor = cursor_page(session, stmt, Lesson, page)
    return Page(items=[LessonOut.model_validate(item) for item in items], next_cursor=next_cursor)
