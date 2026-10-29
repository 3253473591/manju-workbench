"""集与片段。写操作一律要 `If-Match`；读任何登录用户可见。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from manju import clip_rules, readiness
from manju.deps import (
    Paging,
    PathId,
    check_version,
    cursor_page,
    current_user,
    get_session,
    paging,
    parse_if_match,
    project_for_read,
    project_for_write,
)
from manju.errors import ApiError, ErrorCode
from manju.models import Asset, Clip, Episode, Revision, User
from manju.schemas import (
    ApiModel,
    ClipCreate,
    ClipOut,
    ClipUpdate,
    EpisodeCreate,
    EpisodeOut,
    EpisodeUpdate,
    Page,
    PublishOut,
    ReadinessOut,
)

router = APIRouter(tags=["content"])


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


DERIVED_SLOT_FIELDS = frozenset({"url", "thumb_url"})


def _strip_derived(items: list[dict] | None) -> list[dict] | None:
    """出参派生的取图地址一律不落库 —— 存下来换存储后就成死链了。

    这是"派生字段不进 JSON 列"的唯一一处规则，`_dump`（创建）与 patch 都走它。
    """
    if not items:
        return None
    return [{key: value for key, value in item.items() if key not in DERIVED_SLOT_FIELDS} for item in items]


def _dump(items: list | None) -> list[dict] | None:
    """pydantic 模型 → 可直接落 JSON 列的 dict（秒/金额保持字符串，避免浮点漂移）。"""
    if not items:
        return None
    return _strip_derived([item.model_dump(mode="json") for item in items])


def _episode_or_404(session: Session, episode_id: int) -> Episode:
    episode = session.get(Episode, episode_id)
    if episode is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    return episode


def _clip_or_404(session: Session, clip_id: int) -> Clip:
    clip = session.get(Clip, clip_id)
    if clip is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    return clip


def _episode_for_read(session: Session, user: User, episode_id: int) -> Episode:
    episode = _episode_or_404(session, episode_id)
    project_for_read(session, user, episode.project_id)
    return episode


def _episode_for_write(session: Session, user: User, episode_id: int) -> Episode:
    episode = _episode_or_404(session, episode_id)
    project_for_write(session, user, episode.project_id)
    return episode


def _clip_for_read(session: Session, user: User, clip_id: int) -> Clip:
    clip = _clip_or_404(session, clip_id)
    _episode_for_read(session, user, clip.episode_id)
    return clip


def _clip_for_write(session: Session, user: User, clip_id: int) -> Clip:
    clip = _clip_or_404(session, clip_id)
    _episode_for_write(session, user, clip.episode_id)
    return clip


def _clip_out(
    clip: Clip,
    *,
    output_asset_id: int | None = None,
    assets: dict[int, Asset] | None = None,
    cost_total: Decimal | None = None,
) -> ClipOut:
    return ClipOut(
        id=clip.id,
        episode_id=clip.episode_id,
        idx=clip.idx,
        source_idx=clip.source_idx,
        title=clip.title,
        duration_s=clip.duration_s,
        status=clip.status,
        prompt=clip.prompt,
        negative=clip.negative,
        shots=clip.shots_json,
        ref_slots=[_slot_out(slot, assets or {}) for slot in (clip.ref_slots_json or [])] or None,
        dialogue=clip.dialogue_json,
        acceptance=clip.acceptance_json,
        current_revision_id=clip.current_revision_id,
        output_asset_id=output_asset_id,
        cost_total=cost_total or Decimal(0),
        parent_clip_id=clip.parent_clip_id,
        version=clip.version,
        created_at=clip.created_at,
        updated_at=clip.updated_at,
    )


def _slot_out(slot: dict, assets: dict[int, Asset]) -> dict:
    """槽位带出取图地址（`02` §3.3）：`asset_id` 有值就直接给出，前端不用再查一次资产。"""
    asset = assets.get(slot.get("asset_id"))
    if asset is None:
        return slot
    return {
        **slot,
        "url": f"/api/v1/assets/{asset.id}/file",
        "thumb_url": f"/api/v1/assets/{asset.id}/file?thumb=1",
    }


def _slot_assets(session: Session, clips: list[Clip]) -> dict[int, Asset]:
    ids = {
        slot["asset_id"]
        for clip in clips
        for slot in (clip.ref_slots_json or [])
        if slot.get("asset_id")
    }
    if not ids:
        return {}
    return {asset.id: asset for asset in session.scalars(select(Asset).where(Asset.id.in_(ids)))}


def _cost_totals(session: Session, clip_ids: list[int]) -> dict[int, Decimal]:
    """每片段累计成本（`02` §3.6）。台账只认 `revision.cost`，不重算。"""
    if not clip_ids:
        return {}
    rows = session.execute(
        select(Revision.target_id, func.coalesce(func.sum(Revision.cost), 0))
        .where(Revision.target_kind == "clip", Revision.target_id.in_(clip_ids))
        .group_by(Revision.target_id)
    ).all()
    return {clip_id: Decimal(total) for clip_id, total in rows}


def _output_assets(session: Session, clip_ids: list[int]) -> dict[int, int | None]:
    """`clip.id → revision.output_asset_id`，一次查询喂满整页，避免 N+1。"""
    if not clip_ids:
        return {}
    rows = session.execute(
        select(Clip.id, Revision.output_asset_id)
        .join(Revision, Revision.id == Clip.current_revision_id)
        .where(Clip.id.in_(clip_ids))
    ).all()
    return {clip_id: asset_id for clip_id, asset_id in rows}


def _clip_views(session: Session, clips: list[Clip]) -> list[ClipOut]:
    """三个派生字段各查一次，喂满整页：`output_asset_id` / `cost_total` / 槽位取图地址。"""
    ids = [clip.id for clip in clips]
    outputs = _output_assets(session, ids)
    costs = _cost_totals(session, ids)
    assets = _slot_assets(session, clips)
    return [
        _clip_out(
            clip,
            output_asset_id=outputs.get(clip.id),
            assets=assets,
            cost_total=costs.get(clip.id),
        )
        for clip in clips
    ]


# ---------------------------------------------------------------- 集


@router.get("/projects/{project_id}/episodes")
def list_episodes(
    project_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[EpisodeOut]:
    project_for_read(session, user, project_id)
    rows = session.scalars(select(Episode).where(Episode.project_id == project_id).order_by(Episode.idx))
    return [EpisodeOut.model_validate(row) for row in rows]


@router.post("/projects/{project_id}/episodes", status_code=201)
def create_episode(
    project_id: PathId,
    payload: EpisodeCreate,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> EpisodeOut:
    project = project_for_write(session, user, project_id)
    session.refresh(project, with_for_update=True)
    project_for_write(session, user, project_id)
    indices = session.scalars(select(Episode.idx).where(Episode.project_id == project_id).with_for_update()).all()
    used = max(indices, default=0)
    if used >= 50:
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"reason": "一个项目最多 50 集"})
    episode = Episode(
        project_id=project_id,
        idx=used + 1,
        title=payload.title,
        synopsis=payload.synopsis,
        status="draft",
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(episode)
    session.commit()
    response.headers["ETag"] = f'"{episode.version}"'
    return EpisodeOut.model_validate(episode)


@router.delete("/episodes/{episode_id}", status_code=204)
def delete_episode(
    episode_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    """删集：该集的片段/资产引用随外键级联（`clip.episode_id` ON DELETE CASCADE）。

    删掉后**不重编号**：`idx` 留洞是可见的（第1/第3集），比"悄悄把第3集变成第2集"更诚实 ——
    重编号会让已发出去的链接、外部引用、人脑里的集号一起错位。要无缝，用 `move`。
    """
    episode = _episode_for_write(session, user, episode_id)
    clip_ids = select(Clip.id).where(Clip.episode_id == episode.id)
    paid = session.scalar(
        select(func.count()).select_from(Revision).where(
            Revision.target_kind == "clip", Revision.target_id.in_(clip_ids)
        )
    )
    if paid:
        # revision 是 append-only 的付费凭证，没有 FK 跟着级联；删集会让它们变成无主孤儿，
        # 而 `cost` 端点 join 不到 → 台账凭空少一块（`03` §3：宁可偏高，不可漏记）
        raise ApiError(
            ErrorCode.CONFLICT_HAS_REVISIONS,
            {"reason": "该集已有付费产物，删集会让台账漏记；先归档项目或保留该集", "revisions": paid},
        )
    session.delete(episode)
    session.commit()


class EpisodeMoveIn(ApiModel):
    to_idx: Annotated[int, Field(ge=1, le=50)]


@router.post("/episodes/{episode_id}/move")
def move_episode(
    episode_id: PathId,
    payload: EpisodeMoveIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[EpisodeOut]:
    """把某一集挪到 `to_idx`，其余顺移 —— 返回该项目的完整集列表（前端直接替换）。

    实现是**插入式平移**，不是"先改成负数再落位"：`idx` 是 `INT UNSIGNED` 且有 `CHECK 1..50`，
    占位值两边都不合法；而按"只会挪进刚腾出的槽位"的顺序写，既满足 CHECK 也永不撞 `uk_ep_idx`
    （往前移就从后往前走，往后移就从前走后走）。
    """
    episode = _episode_for_write(session, user, episode_id)
    episodes = list(
        session.scalars(
            select(Episode).where(Episode.project_id == episode.project_id).order_by(Episode.idx).with_for_update()
        )
    )
    current = episode.idx
    target = max(1, min(payload.to_idx, len(episodes)))
    if target == current:
        return [EpisodeOut.model_validate(item) for item in episodes]

    # 原地置换必须有一个空槽：先把要移动的这集挪进空槽，再平移其他人，最后落位。
    # （`idx` 是 INT UNSIGNED + CHECK 1..50，负数/超界的占位值都不合法）
    used = {row.idx for row in episodes}
    spare = next((slot for slot in range(1, 51) if slot not in used), None)
    if spare is None:
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"reason": "集已满 50，没有空位可用于重排"})
    episode.idx = spare
    session.flush()

    if target < current:
        # 往前移：区间 [target, current-1] 每集 +1，从后往前走（只挪进刚腾出的槽位）
        for item in reversed([row for row in episodes if target <= row.idx < current]):
            item.idx += 1
            item.updated_at = _now()
            session.flush()
    else:
        # 往后移：区间 (current, target] 每集 -1，从前往后走
        for item in [row for row in episodes if current < row.idx <= target]:
            item.idx -= 1
            item.updated_at = _now()
            session.flush()
    episode.idx = target
    episode.updated_at = _now()
    session.commit()
    return [
        EpisodeOut.model_validate(item)
        for item in session.scalars(
            select(Episode).where(Episode.project_id == episode.project_id).order_by(Episode.idx)
        )
    ]


@router.get("/episodes/{episode_id}/readiness")
def get_readiness(
    episode_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ReadinessOut:
    """发布门预览：产物 / 审批 / 质检 / 预算四道检查，返回逐项结果与阻塞项。"""
    episode = _episode_for_read(session, user, episode_id)
    return ReadinessOut(**readiness.episode_readiness(session, episode))


@router.post("/episodes/{episode_id}/publish")
def publish_episode(
    episode_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> PublishOut:
    """把一集标记为 done —— **只有四道检查全过才允许**；不满足返回 409 与逐项原因。

    幂等：已经 done 的集再点发布仍然 200（不会加版本），但检查结果照常返回。
    """
    episode = _episode_for_write(session, user, episode_id)
    result = readiness.episode_readiness(session, episode)
    if not result["ready"]:
        raise ApiError(
            ErrorCode.PUBLISH_BLOCKED,
            {
                "blockers": result["blockers"],
                "checks": result["checks"],
            },
        )
    if episode.status != "done":
        episode.status = "done"
        episode.version += 1
        episode.updated_at = _now()
        session.commit()
    return PublishOut(episode=EpisodeOut.model_validate(episode), readiness=ReadinessOut(**result))


@router.get("/episodes/{episode_id}")
def get_episode(
    episode_id: PathId,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> EpisodeOut:
    episode = _episode_for_read(session, user, episode_id)
    response.headers["ETag"] = f'"{episode.version}"'
    return EpisodeOut.model_validate(episode)


@router.patch("/episodes/{episode_id}")
def patch_episode(
    episode_id: PathId,
    payload: EpisodeUpdate,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> EpisodeOut:
    episode = _episode_for_write(session, user, episode_id)
    check_version(parse_if_match(request.headers.get("If-Match")), episode.version)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(episode, field, value)
    episode.version += 1
    episode.updated_at = _now()
    session.commit()
    response.headers["ETag"] = f'"{episode.version}"'
    return EpisodeOut.model_validate(episode)


# ---------------------------------------------------------------- 片段


@router.get("/projects/{project_id}/clips")
def list_project_clips(
    project_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    page: Paging = Depends(paging),
) -> Page[ClipOut]:
    """项目级镜头墙：跨集分页拉全部片段（`ClipOut.episode_id` 够前端回查集号）。"""
    project_for_read(session, user, project_id)
    clip_ids = select(Clip.id).join(Episode, Episode.id == Clip.episode_id).where(
        Episode.project_id == project_id
    )
    items, next_cursor = cursor_page(session, select(Clip).where(Clip.id.in_(clip_ids)), Clip, page)
    return Page(items=_clip_views(session, items), next_cursor=next_cursor)


@router.get("/episodes/{episode_id}/clips")
def list_clips(
    episode_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[ClipOut]:
    _episode_for_read(session, user, episode_id)
    clips = list(session.scalars(select(Clip).where(Clip.episode_id == episode_id).order_by(Clip.idx)))
    return _clip_views(session, clips)


@router.post("/episodes/{episode_id}/clips", status_code=201)
def create_clip(
    episode_id: PathId,
    payload: ClipCreate,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ClipOut:
    episode = _episode_for_write(session, user, episode_id)
    session.refresh(episode, with_for_update=True)
    indices = set(session.scalars(select(Clip.idx).where(
        Clip.episode_id == episode_id, Clip.parent_clip_id.is_(None)
    ).with_for_update()))
    used = max(indices, default=0)
    idx = used + 1 if used < 30 else next((i for i in range(1, 31) if i not in indices), None)
    if idx is None:
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"reason": "一集最多 30 个片段"})
    shots = _dump(payload.shots)
    slots = _dump(payload.ref_slots)
    dialogue = _dump(payload.dialogue)
    acceptance = _dump(payload.acceptance)
    # draft 阶段允许空分镜；一旦有内容就必须自洽
    clip_rules.enforce_clip(
        session,
        project_id=episode.project_id,
        duration_s=payload.duration_s,
        shots=shots,
        ref_slots=slots,
        dialogue=dialogue,
        acceptance=acceptance,
        prompt=payload.prompt,
        require_shots=False,
    )
    clip = Clip(
        episode_id=episode_id,
        idx=idx,
        source_idx=payload.source_idx or idx,
        title=payload.title,
        duration_s=payload.duration_s,
        status="draft",
        prompt=payload.prompt,
        negative=payload.negative,
        shots_json=shots,
        ref_slots_json=slots,
        dialogue_json=dialogue,
        acceptance_json=acceptance,
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(clip)
    session.commit()
    response.headers["ETag"] = f'"{clip.version}"'
    return _clip_views(session, [clip])[0]


@router.get("/clips/{clip_id}")
def get_clip(
    clip_id: PathId,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ClipOut:
    clip = _clip_for_read(session, user, clip_id)
    response.headers["ETag"] = f'"{clip.version}"'
    return _clip_views(session, [clip])[0]


@router.patch("/clips/{clip_id}")
def patch_clip(
    clip_id: PathId,
    payload: ClipUpdate,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> ClipOut:
    clip = _clip_for_write(session, user, clip_id)
    check_version(parse_if_match(request.headers.get("If-Match")), clip.version)
    episode = _episode_or_404(session, clip.episode_id)

    changes = payload.model_dump(exclude_unset=True, mode="json")
    for field in ("status", "duration_s"):
        if field in changes and changes[field] is None:
            raise ApiError(ErrorCode.VALIDATION_SHAPE, {"field": field, "reason": "not nullable"})
    target_status = changes.get("status", clip.status)
    if target_status != clip.status:
        clip_rules.enforce_transition(clip.status, target_status)

    shots = changes.get("shots", clip.shots_json)
    slots = changes.get("ref_slots", clip.ref_slots_json)
    dialogue = changes.get("dialogue", clip.dialogue_json)
    acceptance = changes.get("acceptance", clip.acceptance_json)
    prompt = changes.get("prompt", clip.prompt)
    clip_rules.enforce_clip(
        session,
        project_id=episode.project_id,
        duration_s=changes.get("duration_s", clip.duration_s),
        shots=shots,
        ref_slots=slots,
        dialogue=dialogue,
        acceptance=acceptance,
        prompt=prompt,
        require_shots=target_status != "draft",
    )

    json_fields = {
        "shots": "shots_json",
        "ref_slots": "ref_slots_json",
        "dialogue": "dialogue_json",
        "acceptance": "acceptance_json",
    }
    for field, value in changes.items():
        if field in json_fields:
            setattr(clip, json_fields[field], _strip_derived(value))
        elif field == "duration_s":
            clip.duration_s = Decimal(value)
        else:
            setattr(clip, field, value)
    clip.version += 1
    clip.updated_at = _now()
    session.commit()
    response.headers["ETag"] = f'"{clip.version}"'
    return _clip_views(session, [clip])[0]


@router.delete("/clips/{clip_id}", status_code=204)
def delete_clip(
    clip_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    clip = _clip_for_write(session, user, clip_id)
    paid = session.scalar(
        select(func.count()).select_from(Revision).where(
            Revision.target_kind == "clip", Revision.target_id == clip.id
        )
    )
    if paid:
        raise ApiError(
            ErrorCode.CONFLICT_HAS_REVISIONS,
            {"reason": "该片段已有付费产物，删它会让台账漏记", "revisions": paid},
        )
    if clip.parent_clip_id is None:
        children = session.scalar(select(func.count()).where(Clip.parent_clip_id == clip.id))
        if children:
            raise ApiError(
                ErrorCode.VALIDATION_RANGE,
                {"reason": "该片段有替换件，先删替换件", "children": children},
            )
    session.delete(clip)
    session.commit()
