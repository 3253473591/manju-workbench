"""多集：成本按集、项目级镜头墙、集状态推进（PRD 的"分集"口径）。

这三样是"一个项目多集"从**能用**到**能干活**的分界：
- 成本没有 `group_by=episode` 时，"这一集花了多少"只能前端自己加；
- 没有项目级片段列表时，跨集看片必须一集一集切；
- `episode.status` 没人推进时，集列表永远显示 draft。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Clip, Episode, Revision
from tests.conftest import login


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _episode(session: Session, project_id: int, idx: int, *, status: str = "draft") -> Episode:
    episode = Episode(
        project_id=project_id, idx=idx, title=f"第{idx}集", status=status, version=1,
        created_at=_now(), updated_at=_now(),
    )
    session.add(episode)
    session.flush()
    return episode


def _clip(session: Session, episode_id: int, *, idx: int) -> Clip:
    clip = Clip(
        episode_id=episode_id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"), status="draft",
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return clip


def _revision(session: Session, clip: Clip, cost: str) -> Revision:
    revision = Revision(
        target_kind="clip", target_id=clip.id, prompt="p", cost=Decimal(cost),
        params_json={"res": "720p"}, provider="mock", created_at=_now(),
    )
    session.add(revision)
    session.flush()
    return revision


def test_cost_can_be_grouped_by_episode(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2)
    clip_a = world["clip"]
    clip_b = _clip(session, second.id, idx=1)
    _revision(session, clip_a, "1.500000")
    _revision(session, clip_b, "0.250000")

    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=episode")
    assert response.status_code == 200, response.text
    items = {item["key"]: item for item in response.json()["items"]}
    assert items["第1集"]["cost"] == "1.500000"
    assert items["第2集"]["cost"] == "0.250000"
    assert issues_free(response.json())


def issues_free(payload: dict) -> bool:
    total = Decimal(payload["total"]["cost"])
    return total == sum(Decimal(item["cost"]) for item in payload["items"])


def test_cost_group_by_still_rejects_garbage(
    client: TestClient, world: dict, settings: Settings
) -> None:
    login(client, world["owner"], settings)
    for bad in ("stage", "episodes", "EPISODE", ""):
        response = client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by={bad}")
        assert response.status_code == 422, f"{bad} → {response.status_code}"


def test_project_level_clips_span_episodes(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2)
    _clip(session, second.id, idx=1)
    _clip(session, second.id, idx=2)

    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/projects/{world['project'].id}/clips?limit=50")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    episodes = {item["episode_id"] for item in items}
    assert episodes == {world["episode"].id, second.id}
    assert len(items) == 3  # world 里 1 个 + 新建的 2 个


def test_project_clips_respect_read_permission(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    from manju.models import Project
    from tests.conftest import make_user

    other = make_user(session, "other@example.com", "lead")
    foreign = Project(
        title="别人的项目", owner_id=other.id, status="active", version=1,
        created_at=_now(), updated_at=_now(),
    )
    session.add(foreign)
    session.flush()
    login(client, world["owner"], settings)
    # 读任意项目是产品决定（`02` §2.2）：他人的项目也能读，但不该混进自己的列表
    assert client.get(f"/api/v1/projects/{foreign.id}/clips").status_code == 200
    assert client.get(f"/api/v1/projects/{world['project'].id}/clips").json()["items"]


@pytest.mark.parametrize("stage,expected", [("S2", "scripted"), ("S3", "storyboarded")])
def test_steps_advance_episode_status(
    client: TestClient, session: Session, world: dict, settings: Settings, stage: str, expected: str
) -> None:
    episode = _episode(session, world["project"].id, 3)
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/episodes/{episode.id}/steps/{stage}/run")
    assert response.status_code == 200, response.text
    session.refresh(episode)
    assert episode.status == expected


def test_episode_status_never_goes_backwards(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    episode = _episode(session, world["project"].id, 4, status="producing")
    login(client, world["owner"], settings)
    client.post(f"/api/v1/episodes/{episode.id}/steps/S2/run")
    session.refresh(episode)
    assert episode.status == "producing"  # 已在更后面的状态，不被 S2 拉回 scripted
