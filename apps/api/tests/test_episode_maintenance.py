"""集的删除与重排（多集维护）。

两条相反的取向，各自都有用例：
- **删除不重编号**：`idx` 留洞（第1/第3集）比"悄悄把第3集变成第2集"诚实 —— 集号会出现在链接、
  文档、人脑里，静默改动会让它们一起错位；
- **重排是显式的**：`POST /episodes/{eid}/move {to_idx}`，两阶段写（先负数占位）绕开 `uk_ep_idx`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Clip, Episode
from tests.conftest import login


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _episode(session: Session, project_id: int, idx: int, title: str) -> Episode:
    episode = Episode(
        project_id=project_id, idx=idx, title=title, status="draft", version=1,
        created_at=_now(), updated_at=_now(),
    )
    session.add(episode)
    session.flush()
    return episode


def _clip(session: Session, episode_id: int, idx: int = 1) -> Clip:
    clip = Clip(
        episode_id=episode_id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"), status="draft",
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return clip


def test_delete_episode_cascades_clips(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2, "第二集")
    _clip(session, second.id)
    login(client, world["owner"], settings)

    response = client.delete(f"/api/v1/episodes/{second.id}")
    assert response.status_code == 204
    assert session.get(Episode, second.id) is None
    assert session.scalars(select(Clip).where(Clip.episode_id == second.id)).all() == []
    # 第一集不受影响
    assert session.get(Episode, world["episode"].id) is not None


def test_delete_keeps_gaps_instead_of_renumbering(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2, "第二集")
    third = _episode(session, world["project"].id, 3, "第三集")
    login(client, world["owner"], settings)
    assert client.delete(f"/api/v1/episodes/{second.id}").status_code == 204
    session.refresh(third)
    assert third.idx == 3  # 不重编号：留洞是刻意的
    assert second.id != third.id


def test_move_episode_renumbers_contiguously(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    _episode(session, world["project"].id, 2, "第二集")
    third = _episode(session, world["project"].id, 3, "第三集")
    login(client, world["owner"], settings)

    # 把第 3 集挪到第 1 位
    response = client.post(f"/api/v1/episodes/{third.id}/move", json={"to_idx": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["idx"] for item in body] == [1, 2, 3]
    assert body[0]["id"] == third.id
    # 落库一致
    rows = session.scalars(
        select(Episode).where(Episode.project_id == world["project"].id).order_by(Episode.idx)
    ).all()
    assert [row.id for row in rows] == [item["id"] for item in body]


def test_move_is_clamped_to_range(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2, "第二集")
    login(client, world["owner"], settings)
    assert client.post(f"/api/v1/episodes/{second.id}/move", json={"to_idx": 0}).status_code == 422
    assert client.post(f"/api/v1/episodes/{second.id}/move", json={"to_idx": 51}).status_code == 422
    # 超出末尾 → 落到最后一位（不报错，体验优先）
    clamped = client.post(f"/api/v1/episodes/{second.id}/move", json={"to_idx": 50})
    assert clamped.status_code == 200
    assert clamped.json()[-1]["id"] == second.id


def test_episode_maintenance_requires_write(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2, "第二集")
    login(client, world["viewer"], settings)
    assert client.delete(f"/api/v1/episodes/{second.id}").status_code == 403
    assert client.post(f"/api/v1/episodes/{second.id}/move", json={"to_idx": 1}).status_code == 403


def test_episode_maintenance_missing_target(
    client: TestClient, world: dict, settings: Settings
) -> None:
    login(client, world["owner"], settings)
    assert client.delete("/api/v1/episodes/999999").status_code == 404
    assert client.post("/api/v1/episodes/999999/move", json={"to_idx": 1}).status_code == 404
