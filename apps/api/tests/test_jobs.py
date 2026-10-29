"""生成与 job（`02` §3.5 / §5.3 / §6.1，`03` §8）。

钉的都是"写错了前端就会误判"的地方：幂等键与 `If-Match` 的先后、拒绝付费点是否零计费、
预算穿线后这笔钱还在不在台账上、重复提交会不会双扣。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from manju import jobs
from manju.config import Settings
from manju.models import Clip, Job, Revision
from manju.providers import MockProvider
from tests.conftest import login

GENERATE_HEADERS = {"If-Match": '"1"', "Idempotency-Key": "gen-1"}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _fixture(media_root: Path) -> None:
    """MockProvider 会从 MEDIA_ROOT/第一集 里挑一个 mp4 当产物。"""
    folder = media_root / "第一集"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "s1_demo.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)


def _generate(client: TestClient, clip_id: int, **overrides) -> "object":
    headers = dict(GENERATE_HEADERS)
    headers.update(overrides.pop("headers", {}))
    body = {"res": "draft"}
    body.update(overrides.pop("json", {}))
    return client.post(f"/api/v1/clips/{clip_id}/generate", json=body, headers=headers, **overrides)


def test_generate_requires_idempotency_key(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    response = client.post(
        f"/api/v1/clips/{world['clip'].id}/generate", json={"res": "draft"}, headers={"If-Match": '"1"'}
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"]["header"] == "Idempotency-Key"


def test_generate_default_mode_stops_at_pay_point(
    client: TestClient, world: dict, settings: Settings, media_root: Path
) -> None:
    _fixture(media_root)
    login(client, world["owner"], settings)
    response = _generate(client, world["clip"].id)
    assert response.status_code == 202
    body = response.json()
    assert body["job"]["state"] == "waiting_confirm"
    assert body["job"]["cost_used"] == "0.000000"
    assert body["preview"]["cost_est"] == "2.270004"  # 0.189167 × 12 s
    assert body["preview"]["tokens_est"] == 260_100
    assert body["preview"]["prompt_head"]


def test_cancel_at_pay_point_costs_nothing(
    client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path
) -> None:
    _fixture(media_root)
    login(client, world["owner"], settings)
    job_id = _generate(client, world["clip"].id).json()["job"]["id"]
    cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["cost_used"] == "0.000000"
    assert session.scalar(select(Revision).where(Revision.target_id == world["clip"].id)) is None


def test_confirm_then_worker_completes_and_bills(
    client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path
) -> None:
    _fixture(media_root)
    login(client, world["owner"], settings)
    job_id = _generate(client, world["clip"].id).json()["job"]["id"]
    assert client.post(f"/api/v1/jobs/{job_id}/confirm").json()["state"] == "queued"

    assert jobs.run_pending(session, settings, "test-worker") == 1

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["state"] == "done"
    assert job["cost_used"] == "2.270004"
    assert job["model"] == "seedance-2.0-mini"

    revision = session.scalars(select(Revision).where(Revision.target_id == world["clip"].id)).one()
    assert revision.cost == Decimal("2.270004")
    assert revision.input_tokens == 260_100
    assert revision.output_asset_id is not None
    assert revision.motion_json["freeze"] == 0

    clip = client.get(f"/api/v1/clips/{world['clip'].id}").json()
    # 这个片段**没有验收点**：没有可机检项就不该把出片堵在 review（trace 记 `no_criteria`）。
    # 有验收点但判不了才停在 review —— 见 `test_trace.py` 的两条对照用例。
    assert clip["status"] == "approved"
    assert clip["current_revision_id"] == revision.id
    assert clip["cost_total"] == "2.270004"
    assert clip["output_asset_id"] == revision.output_asset_id


def test_idempotent_replay_returns_same_job(
    client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path
) -> None:
    _fixture(media_root)
    login(client, world["owner"], settings)
    first = _generate(client, world["clip"].id)
    second = _generate(client, world["clip"].id)
    assert first.status_code == 202 and second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert first.json()["job"]["id"] == second.json()["job"]["id"]
    assert len(session.scalars(select(Job)).all()) == 1


def test_replay_with_stale_if_match_still_replays(
    client: TestClient, world: dict, settings: Settings, media_root: Path
) -> None:
    """顺序回归用例：幂等键必须在 `If-Match` 之前 —— 否则超时重试永远 412。"""
    _fixture(media_root)
    login(client, world["owner"], settings)
    _generate(client, world["clip"].id)
    replay = _generate(client, world["clip"].id, headers={"If-Match": '"999"'})
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"


def test_same_key_different_body_conflicts(
    client: TestClient, world: dict, settings: Settings, media_root: Path
) -> None:
    _fixture(media_root)
    login(client, world["owner"], settings)
    _generate(client, world["clip"].id)
    conflict = _generate(client, world["clip"].id, json={"res": "final"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEM_KEY_REUSED"


def test_generate_requires_if_match(client: TestClient, world: dict, settings: Settings, media_root: Path) -> None:
    _fixture(media_root)
    login(client, world["owner"], settings)
    response = client.post(
        f"/api/v1/clips/{world['clip'].id}/generate",
        json={"res": "draft"},
        headers={"Idempotency-Key": "gen-x"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"]["header"] == "If-Match"


def test_budget_exceeded_after_call_still_records_cost(
    client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path
) -> None:
    """预算穿线发生在花钱之后：`revision` 照落，job 停 `paused_budget`（`03` §3 计费铁律）。"""
    _fixture(media_root)
    login(client, world["owner"], settings)
    response = _generate(client, world["clip"].id, json={"res": "draft", "budget_limit": "1.00"})
    job_id = response.json()["job"]["id"]
    client.post(f"/api/v1/jobs/{job_id}/confirm")

    class UnderestimatedProvider(MockProvider):
        def estimate(self, seconds):
            return Decimal("0.50")

    provider = UnderestimatedProvider(Path("第一集/s1_demo.mp4"))
    assert jobs.run_pending(session, settings, "test-worker", provider) == 1
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["state"] == "paused_budget"
    assert job["error_code"] == "BUDGET_EXCEEDED"
    assert job["cost_used"] == "2.270004"

    revision = session.scalars(select(Revision).where(Revision.target_id == world["clip"].id)).one()
    assert revision.cost == Decimal("2.270004")  # 台账没漏这笔钱


def test_second_video_job_in_same_project_is_rejected(
    client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path
) -> None:
    _fixture(media_root)
    other = Clip(
        episode_id=world["episode"].id,
        idx=2,
        source_idx=2,
        duration_s=Decimal("5.00"),
        status="draft",
        prompt="另一个片段",
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(other)
    session.flush()
    login(client, world["owner"], settings)
    first = _generate(client, world["clip"].id)
    assert first.status_code == 202
    client.post(f"/api/v1/jobs/{first.json()['job']['id']}/confirm")  # waiting_confirm 不算"进行中"

    second = _generate(
        client,
        other.id,
        headers={"If-Match": '"1"', "Idempotency-Key": "gen-2"},
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "PROJECT_BUSY"
    assert second.headers["Retry-After"] == "5"


def test_retry_after_failure_increments_attempts(
    client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path
) -> None:
    _fixture(media_root)
    login(client, world["owner"], settings)
    job_id = _generate(client, world["clip"].id).json()["job"]["id"]
    client.post(f"/api/v1/jobs/{job_id}/confirm")
    session.execute(update(Job).where(Job.id == job_id).values(state="failed", attempts=1, locked_by=None))
    session.flush()

    retried = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["state"] == "queued"
    assert retried.json()["attempts"] == 2

    session.execute(update(Job).where(Job.id == job_id).values(state="failed", attempts=3, locked_by=None))
    session.flush()
    exhausted = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert exhausted.status_code == 409
    assert exhausted.json()["error"]["code"] == "JOB_STATE_INVALID"


def test_stale_running_job_is_requeued(
    client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path
) -> None:
    """worker 崩溃：心跳超 120 s 后回到 queued（`03` §2 的 L5）。"""
    _fixture(media_root)
    login(client, world["owner"], settings)
    job_id = _generate(client, world["clip"].id).json()["job"]["id"]
    client.post(f"/api/v1/jobs/{job_id}/confirm")
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(state="running", locked_by="dead#1", heartbeat_at=_now() - timedelta(seconds=600))
    )
    session.flush()

    assert jobs.reap_stale(session, "test-worker") == 1
    assert client.get(f"/api/v1/jobs/{job_id}").json()["state"] == "queued"


def test_job_list_by_target(client: TestClient, world: dict, settings: Settings, media_root: Path) -> None:
    _fixture(media_root)
    login(client, world["owner"], settings)
    _generate(client, world["clip"].id)
    listed = client.get(f"/api/v1/jobs?target_kind=clip&target_id={world['clip'].id}").json()
    assert len(listed["items"]) == 1
    assert listed["items"][0]["target_id"] == world["clip"].id


def test_generate_respects_permission_and_version(
    client: TestClient, world: dict, settings: Settings, media_root: Path
) -> None:
    _fixture(media_root)
    login(client, world["viewer"], settings)
    denied = _generate(client, world["clip"].id)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERM_READONLY_PROJECT"

    login(client, world["owner"], settings)
    stale = _generate(client, world["clip"].id, headers={"If-Match": '"99"'})
    assert stale.status_code == 412
