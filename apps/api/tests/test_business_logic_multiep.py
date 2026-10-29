"""多集 / 跨集口径的业务逻辑撞法（第四轮）。

怀疑点（按"钱和一致性"排）：
1. **删集/删片段会不会让已付的钱从台账里消失** —— revision 是 `target_kind/target_id` 多态，
   没有 FK，剪掉 clip 之后这些行就成了无主孤儿；`cost` 端点 join 不到 → **漏记**。
2. **S2 重跑会不会让剧情与既有分镜不一致** —— 只覆盖 synopsis、片段原样留着，没有任何提示。
3. 集重排后成本归属是否还跟着那一集走。
4. 跨集复用的资产在被引用时是否把两集都列出来。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Asset, Clip, Episode, Revision
from tests.conftest import login


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _episode(session: Session, project_id: int, idx: int, status: str = "draft") -> Episode:
    episode = Episode(
        project_id=project_id, idx=idx, title=f"第{idx}集", status=status, version=1,
        created_at=_now(), updated_at=_now(),
    )
    session.add(episode)
    session.flush()
    return episode


def _clip(session: Session, episode_id: int, idx: int = 1, **extra) -> Clip:
    fields = {
        "episode_id": episode_id, "idx": idx, "source_idx": idx, "duration_s": Decimal("6.00"),
        "status": "approved", "created_at": _now(), "updated_at": _now(),
    }
    fields.update(extra)
    clip = Clip(**fields)
    session.add(clip)
    session.flush()
    return clip


def _paid_take(session: Session, clip: Clip, cost: str) -> Revision:
    revision = Revision(
        target_kind="clip", target_id=clip.id, prompt="p", cost=Decimal(cost),
        params_json={}, provider="mock", created_at=_now(),
    )
    session.add(revision)
    session.flush()
    clip.current_revision_id = revision.id
    session.flush()
    return revision


# ---------------------------------------------------------------- 成本归属


def test_cost_follows_episode_across_reorder(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2)
    _paid_take(session, _clip(session, second.id), "3.000000")
    login(client, world["owner"], settings)

    before = {item["key"]: item["cost"] for item in
              client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=episode").json()["items"]}
    assert before["第2集"] == "3.000000"

    # 把第二集挪到第 1 位：成本应该跟着"这一集"走，只是 key 变成「第1集」
    assert client.post(f"/api/v1/episodes/{second.id}/move", json={"to_idx": 1}).status_code == 200
    after = {item["key"]: item["cost"] for item in
             client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=episode").json()["items"]}
    assert after["第1集"] == "3.000000"
    assert sum(Decimal(v) for v in before.values()) == sum(Decimal(v) for v in after.values())


def test_clip_index_is_independent_per_episode(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2)
    for idx in (1, 2, 3):
        _clip(session, second.id, idx=idx)
    login(client, world["owner"], settings)
    rows = client.get(f"/api/v1/episodes/{second.id}/clips").json()
    assert [row["idx"] for row in rows] == [1, 2, 3]  # 与第一集的 1 不冲突


# ---------------------------------------------------------------- 删集 / 删片段 vs 台账


def test_deleting_episode_with_paid_takes_must_not_silently_drop_cost(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """已付钱的集不能一删了之：revision 是走不掉审计的。

    现状（未修时）：`DELETE /episodes/{eid}` 级联删 clip，revision 因为没有 FK 变成孤儿 →
    `cost` 端点 join 不到 → 项目成本凭空少一块（`03` 的"宁可偏高，不可漏记"被违反）。
    """
    second = _episode(session, world["project"].id, 2)
    paid_clip = _clip(session, second.id)
    _paid_take(session, paid_clip, "2.000000")
    login(client, world["owner"], settings)

    total_before = Decimal(
        client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=model").json()["total"]["cost"]
    )
    response = client.delete(f"/api/v1/episodes/{second.id}")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT_HAS_REVISIONS"
    total_after = Decimal(
        client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=model").json()["total"]["cost"]
    )
    assert total_after == total_before  # 台账一分没少


def test_deleting_clip_with_paid_take_must_not_silently_drop_cost(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2)
    paid_clip = _clip(session, second.id)
    _paid_take(session, paid_clip, "1.500000")
    login(client, world["owner"], settings)

    total_before = Decimal(
        client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=model").json()["total"]["cost"]
    )
    response = client.delete(f"/api/v1/clips/{paid_clip.id}")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT_HAS_REVISIONS"
    total_after = Decimal(
        client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=model").json()["total"]["cost"]
    )
    assert total_after == total_before


# ---------------------------------------------------------------- S2 vs 既有分镜


def test_s2_rerun_on_storyboarded_episode_is_flagged(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """已有分镜的集再跑 S2：剧情会被改写，而片段还是按旧剧情生成的 —— 必须挡住或说清楚。

    现状（未修时）：S2 直接覆盖 synopsis 并 200，`episode.status` 还因为"只进不退"停在
    `storyboarded`，于是**剧情与分镜静默不一致**，没有任何信号。
    """
    episode = _episode(session, world["project"].id, 3, status="storyboarded")
    _clip(session, episode.id, idx=1)
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/episodes/{episode.id}/steps/S2/run")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT_CLIP_INDEX"
    assert "分镜" in response.json()["error"]["detail"]["reason"]


# ---------------------------------------------------------------- 跨集复用


def test_asset_used_by_two_episodes_lists_both(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2)
    asset = Asset(
        project_id=world["project"].id, kind="image", path="00_静态资源/a.png", sha256="c" * 64,
        bytes=4, source="seed", created_at=_now(),
    )
    session.add(asset)
    session.flush()
    slot = [{"idx": 1, "kind": "image", "role": "实景", "asset_id": asset.id, "asset_ref": "a", "note": ""}]
    _clip(session, world["episode"].id, idx=9, ref_slots_json=slot)
    _clip(session, second.id, idx=1, ref_slots_json=slot)
    login(client, world["owner"], settings)

    response = client.delete(f"/api/v1/assets/{asset.id}")
    assert response.status_code == 409
    episodes = {usage["episode_id"] for usage in response.json()["error"]["detail"]["usages"]}
    assert episodes == {world["episode"].id, second.id}
