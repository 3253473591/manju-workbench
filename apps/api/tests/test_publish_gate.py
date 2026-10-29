"""发布门与预算档位：四道检查（产物 / 审批 / 质检 / 预算）。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.constants import BUDGET_PROJECT_DAILY
from manju.models import Asset, Clip, Episode, Project, Revision
from tests.conftest import login, make_user


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _episode(session: Session, project_id: int, idx: int = 5) -> Episode:
    episode = Episode(
        project_id=project_id, idx=idx, title="发布门测试集", status="storyboarded", version=1,
        created_at=_now(), updated_at=_now(),
    )
    session.add(episode)
    session.flush()
    return episode


def _product(session: Session, clip: Clip, *, project_id: int, cost: str = "0") -> Revision:
    asset = Asset(
        project_id=project_id, kind="video", path=f".gen/cgt-{clip.id}.mp4", sha256=f"{clip.id:064d}",
        bytes=1, source="gen", created_at=_now(),
    )
    session.add(asset)
    session.flush()
    revision = Revision(
        target_kind="clip", target_id=clip.id, prompt="p", cost=Decimal(cost), params_json={},
        provider="mock", output_asset_id=asset.id, created_at=_now(),
    )
    session.add(revision)
    session.flush()
    clip.current_revision_id = revision.id
    session.flush()
    return revision


def _clip(session: Session, episode: Episode, idx: int, *, status: str, results: list[dict] | None) -> Clip:
    clip = Clip(
        episode_id=episode.id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"), status=status,
        prompt="镜头1（0-6秒）：远景", acceptance_json=[{"id": "a1", "criterion": "画面有推进"}],
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    revision = _product(session, clip, project_id=episode.project_id)
    revision.acceptance_result_json = results
    session.flush()
    return clip


def _ready_episode(session: Session, world: dict) -> Episode:
    episode = _episode(session, world["project"].id)
    _clip(session, episode, 1, status="approved", results=[{"id": "a1", "result": "pass", "score": 88, "evidence": "ok"}])
    _clip(session, episode, 2, status="approved", results=[{"id": "a1", "result": "pass", "score": 84, "evidence": "ok"}])
    return episode


def test_readiness_ready_and_publish(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    episode = _ready_episode(session, world)
    login(client, world["owner"], settings)

    preview = client.get(f"/api/v1/episodes/{episode.id}/readiness")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["ready"] is True
    assert body["blockers"] == []
    assert {item["id"] for item in body["checks"]} == {"assets", "approval", "quality", "budget"}

    published = client.post(f"/api/v1/episodes/{episode.id}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["episode"]["status"] == "done"
    again = client.post(f"/api/v1/episodes/{episode.id}/publish")
    assert again.status_code == 200
    session.refresh(episode)
    assert episode.version == 2, "重复发布不应该再加版本"


def test_readiness_blocks_review_clip(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    episode = _episode(session, world["project"].id, idx=6)
    _clip(session, episode, 1, status="review", results=[{"id": "a1", "result": "unknown", "evidence": "看不清"}])
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/episodes/{episode.id}/publish")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PUBLISH_BLOCKED"
    assert "approval" in error["detail"]["blockers"]


def test_readiness_blocks_failed_criteria(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    episode = _episode(session, world["project"].id, idx=7)
    _clip(session, episode, 1, status="approved", results=[{"id": "a1", "result": "fail", "score": 20, "evidence": "定镜"}])
    login(client, world["owner"], settings)
    body = client.get(f"/api/v1/episodes/{episode.id}/readiness").json()
    assert body["ready"] is False
    assert "quality" in body["blockers"]
    quality = next(item for item in body["checks"] if item["id"] == "quality")
    assert quality["detail"]["failed"] == 1


def test_readiness_unknown_ratio(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """未判率超过 20% 要拦住；恰好 20% 可以通过（边界按 ≤ 处理）。"""
    episode = _episode(session, world["project"].id, idx=8)
    _clip(
        session, episode, 1, status="approved",
        results=[{"id": "a1", "result": "pass", "score": 90, "evidence": "ok"},
                 {"id": "a2", "result": "unknown", "evidence": "看不清"}],
    )
    login(client, world["owner"], settings)
    body = client.get(f"/api/v1/episodes/{episode.id}/readiness").json()
    quality = next(item for item in body["checks"] if item["id"] == "quality")
    assert quality["detail"]["unknown_ratio"] == 0.5
    assert quality["ok"] is False


def test_readiness_blocks_missing_output(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    episode = _episode(session, world["project"].id, idx=9)
    clip = Clip(
        episode_id=episode.id, idx=1, source_idx=1, duration_s=Decimal("6.00"), status="approved",
        prompt="p", created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    login(client, world["owner"], settings)
    body = client.get(f"/api/v1/episodes/{episode.id}/readiness").json()
    assets = next(item for item in body["checks"] if item["id"] == "assets")
    assert assets["ok"] is False and assets["detail"]["with_output"] == 0


def test_budget_over_blocks_publish(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    episode = _ready_episode(session, world)
    spent = session.get(Episode, episode.id)
    clip = session.query(Clip).filter_by(episode_id=episode.id).first()
    session.add(
        Revision(
            target_kind="clip", target_id=clip.id, prompt="p", cost=BUDGET_PROJECT_DAILY,
            params_json={}, provider="mock", created_at=_now(),
        )
    )
    session.flush()
    login(client, world["owner"], settings)
    body = client.get(f"/api/v1/episodes/{episode.id}/readiness").json()
    budget = next(item for item in body["checks"] if item["id"] == "budget")
    assert budget["detail"]["tier"] == "over"
    assert budget["ok"] is False
    assert "budget" in body["blockers"]


def test_cost_endpoint_reports_budget_tiers(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    clip = world["clip"]
    login(client, world["owner"], settings)

    ok = client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=model").json()
    assert ok["budget"]["tier"] == "ok"

    warn_cost = (BUDGET_PROJECT_DAILY * Decimal("0.75")).quantize(Decimal("0.000001"))
    session.add(
        Revision(
            target_kind="clip", target_id=clip.id, prompt="p", cost=warn_cost,
            params_json={}, provider="mock", created_at=_now(),
        )
    )
    session.flush()
    warn = client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=model").json()
    assert warn["budget"]["tier"] == "warn"

    session.add(
        Revision(
            target_kind="clip", target_id=clip.id, prompt="p", cost=BUDGET_PROJECT_DAILY,
            params_json={}, provider="mock", created_at=_now(),
        )
    )
    session.flush()
    over = client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=model").json()
    assert over["budget"]["tier"] == "over"


def test_publish_requires_write(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    episode = _ready_episode(session, world)
    login(client, world["viewer"], settings)
    assert client.post(f"/api/v1/episodes/{episode.id}/publish").status_code == 403
    assert client.get(f"/api/v1/episodes/{episode.id}/readiness").status_code == 200
