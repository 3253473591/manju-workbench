"""reroll：打回后重发（`02` §6.2 的 `rejected ──reroll──▶ generating`）。

它和首次生成共用一条提交路径，差别只有两处：**源状态必须是 `rejected`**、**请求指纹里带
`reroll`**（否则同一个幂等键会把"首次生成"误重放成"重发"）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import jobs
from manju.config import Settings
from manju.models import Clip, Job, Revision
from manju.providers import MockProvider
from tests.conftest import login


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def media(media_root: Path) -> Path:
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    return media_root


def _clip(session: Session, world: dict, *, status: str = "rejected", idx: int = 30) -> Clip:
    clip = Clip(
        episode_id=world["episode"].id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"),
        status=status, prompt="镜头1（0-6秒）：远景急推",
        shots_json=[{"no": 1, "start": 0, "end": 6, "desc": "远景急推"}],
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return clip


def _reroll(client: TestClient, clip: Clip, key: str = "reroll-1", budget: str = "12.000000"):
    return client.post(
        f"/api/v1/clips/{clip.id}/reroll?mode=auto",
        json={"res": "draft", "budget_limit": budget},
        headers={"Idempotency-Key": key, "If-Match": f'"{clip.version}"'},
    )


def test_reroll_submits_job_for_rejected_clip(
    client: TestClient, session: Session, world: dict, settings: Settings, media: Path
) -> None:
    clip = _clip(session, world)
    login(client, world["owner"], settings)
    response = _reroll(client, clip)
    assert response.status_code == 202, response.text
    job = response.json()["job"]
    assert job["state"] == "queued"
    assert job["target_id"] == clip.id


def test_reroll_requires_rejected_state(
    client: TestClient, session: Session, world: dict, settings: Settings, media: Path
) -> None:
    for offset, status in enumerate(("draft", "prompted", "review", "approved")):
        clip = _clip(session, world, status=status, idx=20 + offset)
        login(client, world["owner"], settings)
        response = _reroll(client, clip, key=f"reroll-{status}")
        assert response.status_code == 409, f"{status} → {response.status_code}"
        assert response.json()["error"]["code"] == "JOB_STATE_INVALID"


def test_reroll_replays_with_same_key_but_conflicts_with_generate(
    client: TestClient, session: Session, world: dict, settings: Settings, media: Path
) -> None:
    """同一个幂等键：重发两次是重放；先重发再拿去"首次生成"必须 409。"""
    clip = _clip(session, world)
    login(client, world["owner"], settings)
    first = _reroll(client, clip, key="same-key")
    assert first.status_code == 202

    replay = _reroll(client, clip, key="same-key")
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"

    # 换路径用同一个键 → 指纹不同（body 多了 reroll），必须显式冲突而不是静默重放
    hijack = client.post(
        f"/api/v1/clips/{clip.id}/generate?mode=auto",
        json={"res": "draft", "budget_limit": "12.000000"},
        headers={"Idempotency-Key": "same-key", "If-Match": f'"{clip.version}"'},
    )
    assert hijack.status_code == 409, hijack.text
    assert hijack.json()["error"]["code"] == "IDEM_KEY_REUSED"


def test_reroll_records_parent_revision_chain(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次 take 的 `parent_revision_id` 指向第一次 —— 重发链路在数据里可追。"""
    monkeypatch.setattr(jobs, "evaluate", lambda clip, path: jobs.review.judge([], None, None))
    clip = _clip(session, world, status="prompted")
    job, _ = jobs.submit(
        session, user_id=world["owner"].id, clip=clip, budget_limit=Decimal("12.000000"),
        mode="auto", idem_header="chain-1", payload={"clip_id": clip.id},
    )
    session.flush()
    jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4")))
    session.refresh(clip)
    first = session.get(Revision, clip.current_revision_id)
    assert first.parent_revision_id is None  # 首个 take 没有父级

    # 打回 → 重发
    clip.status = "rejected"
    session.flush()
    job2, _ = jobs.submit(
        session, user_id=world["owner"].id, clip=clip, budget_limit=Decimal("12.000000"),
        mode="auto", idem_header="chain-2", payload={"clip_id": clip.id, "reroll": True},
    )
    session.flush()
    jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4")))
    session.refresh(clip)
    second = session.get(Revision, clip.current_revision_id)
    assert second.id != first.id
    assert second.parent_revision_id == first.id


def test_reroll_requires_idempotency_key_and_if_match(
    client: TestClient, session: Session, world: dict, settings: Settings, media: Path
) -> None:
    clip = _clip(session, world)
    login(client, world["owner"], settings)
    no_key = client.post(
        f"/api/v1/clips/{clip.id}/reroll",
        json={"res": "draft"},
        headers={"If-Match": f'"{clip.version}"'},
    )
    assert no_key.status_code == 422
    no_match = client.post(
        f"/api/v1/clips/{clip.id}/reroll",
        json={"res": "draft"},
        headers={"Idempotency-Key": "no-match"},
    )
    assert no_match.status_code == 422


def test_reroll_is_forbidden_for_viewer(
    client: TestClient, session: Session, world: dict, settings: Settings, media: Path
) -> None:
    clip = _clip(session, world)
    login(client, world["viewer"], settings)
    response = _reroll(client, clip, key="viewer-1")
    assert response.status_code == 403
