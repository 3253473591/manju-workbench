"""clip 入库前的不变量校验（`01` §5.4）。

`domain` 只认纯数据；这里负责把需要查库的部分（参考槽位指向的 asset 是否同项目、kind 是否一致）
补齐，然后把问题一次性抛成 `VALIDATION_INVARIANT`。

这是 service 层的第一块：api 层只做形状，跨表规则落在这里。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from manju import domain
from manju.errors import ApiError, ErrorCode
from manju.models import Asset


def enforce_clip(
    session: Session,
    *,
    project_id: int,
    duration_s: object,
    shots: list[dict] | None,
    ref_slots: list[dict] | None,
    dialogue: list[dict] | None,
    acceptance: list[dict] | None,
    prompt: str | None,
    require_shots: bool,
    capacity: dict[str, int] | None = None,
) -> None:
    """`require_shots` 区分两个口径：draft 阶段允许先建空片段再写分镜，
    但一旦离开 draft（迁到 `prompted` 即"分镜完成"）就必须有镜头。
    """
    problems = [
        *domain.shots_problems(shots, duration_s, require_shots=require_shots),
        *domain.slots_problems(ref_slots, capacity),
        *domain.dialogue_problems(dialogue, shots, prompt),
        *domain.acceptance_problems(acceptance),
        *_asset_problems(session, project_id, ref_slots),
    ]
    if problems:
        raise ApiError(ErrorCode.VALIDATION_INVARIANT, {"problems": problems})


def _asset_problems(session: Session, project_id: int, ref_slots: list[dict] | None) -> list[str]:
    problems = []
    for slot in ref_slots or []:
        asset_id = slot.get("asset_id")
        if asset_id is None:
            continue
        asset = session.get(Asset, asset_id)
        label = f"{slot['kind']} 槽位 {slot['idx']}"
        if asset is None:
            problems.append(f"{label} 引用的 asset {asset_id} 不存在")
            continue
        if asset.project_id != project_id:
            problems.append(f"{label} 引用的 asset {asset_id} 属于别的项目")
        if asset.kind != slot["kind"]:
            problems.append(f"{label} 的 kind={slot['kind']} 与 asset 的 {asset.kind} 不一致")
    return problems


def enforce_transition(current: str, target: str) -> None:
    problems = domain.transition_problems(current, target)
    if problems:
        raise ApiError(ErrorCode.JOB_STATE_INVALID, {"problems": problems})


def enforce_cost(cost: object, unit_price: object | None, video_seconds: object | None, tokens: object | None) -> None:
    problems = domain.cost_problems(cost, unit_price, video_seconds, tokens)
    if problems:
        raise ApiError(ErrorCode.VALIDATION_INVARIANT, {"problems": problems})
