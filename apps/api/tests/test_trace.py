"""P1-4 精简 trace：job 详情页的时间线（`02` §5.4）。

两条要求：**顺序可读**（claim → budget → provider → asset → review → decision）、
**有上限**（每个 job 只留 `TRACE_KEEP` 条，它是面板数据源不是审计日志）。
第三条是踩出来的：失败路径的 trace 必须在事务回滚之后补，否则"为什么失败"永远查不到。
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
from manju.constants import TRACE_KEEP
from manju.models import Clip, Job, JobTrace
from manju.providers import MockProvider, ProviderError
from tests.conftest import login


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def media(media_root: Path) -> Path:
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    return media_root


def _clip(session: Session, world: dict, *, idx: int = 22) -> Clip:
    clip = Clip(
        episode_id=world["episode"].id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"),
        status="prompted", prompt="镜头1（0-6秒）：远景急推",
        shots_json=[{"no": 1, "start": 0, "end": 6, "desc": "远景急推"}],
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return clip


def _run(session: Session, world: dict, settings: Settings, provider, *, idx: int = 22, key: str = "t") -> Job:
    clip = _clip(session, world, idx=idx)
    job, _ = jobs.submit(
        session, user_id=world["owner"].id, clip=clip, budget_limit=Decimal("12.000000"),
        mode="auto", idem_header=f"trace-{key}-{idx}", payload={"clip_id": clip.id},
    )
    session.flush()
    jobs.run_pending(session, settings, "t", provider)
    session.refresh(job)
    return job


def _steps(session: Session, job: Job) -> list[JobTrace]:
    return list(session.scalars(select(JobTrace).where(JobTrace.job_id == job.id).order_by(JobTrace.id)))


def test_successful_job_records_readable_timeline(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "evaluate", lambda clip, path: jobs.review.judge([], None, "降级"))
    job = _run(session, world, settings, MockProvider(Path("第一集/s1_demo.mp4")))
    steps = _steps(session, job)
    assert [row.step for row in steps] == ["claim", "provider", "asset", "review", "decision"]
    assert steps[1].ms is not None and steps[1].ms >= 0
    assert steps[1].detail["cost"] == "1.135002"
    assert steps[2].verdict == "ok" and steps[2].detail["asset_id"]
    assert steps[4].verdict in ("approved", "no_criteria", "awaiting_human")


def test_failed_provider_is_recorded(
    session: Session, settings: Settings, world: dict, media: Path
) -> None:
    class Boom(MockProvider):
        def video(self, *, prompt: str, seconds: Decimal, model: str):
            raise ProviderError("429", retryable=True, status=429)

    job = _run(session, world, settings, Boom(Path("第一集/s1_demo.mp4")))
    steps = _steps(session, job)
    assert [row.step for row in steps] == ["claim", "provider", "decision"]
    provider_step = steps[1]
    assert provider_step.verdict == "error"
    assert provider_step.detail["retryable"] is True
    assert steps[2].verdict == "failed" and steps[2].detail["code"] == "PROVIDER_UNAVAILABLE"


def test_trace_is_trimmed_to_keep_limit(session: Session, settings: Settings, world: dict) -> None:
    clip = _clip(session, world, idx=23)
    job = Job(
        kind="video", target_kind="clip", target_id=clip.id, state="running", attempts=0,
        max_attempts=3, budget_limit=Decimal("12.000000"), cost_used=Decimal(0),
        idem_key="k" * 64, mode="default", created_at=_now(), locked_by="t#1",
    )
    session.add(job)
    session.flush()
    for index in range(TRACE_KEEP + 5):
        jobs.record_trace(session, job, f"step{index}")
    kept = _steps(session, job)
    assert len(kept) == TRACE_KEEP
    assert kept[-1].step == f"step{TRACE_KEEP + 4}"  # 留最新的


def test_trace_endpoint_and_permissions(
    client: TestClient, session: Session, settings: Settings, world: dict, media: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs, "evaluate", lambda clip, path: jobs.review.judge([], None, "降级"))
    job = _run(session, world, settings, MockProvider(Path("第一集/s1_demo.mp4")), idx=24, key="api")

    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/jobs/{job.id}/trace")
    assert response.status_code == 200
    rows = response.json()
    assert [row["step"] for row in rows] == ["claim", "provider", "asset", "review", "decision"]
    assert rows[0]["ts"].endswith("Z") and rows[0]["detail"]["provider"] == "mock"

    # admin 内容只读，但读得到
    login(client, world["admin"], settings)
    assert client.get(f"/api/v1/jobs/{job.id}/trace").status_code == 200
    # 不存在的 job
    assert client.get("/api/v1/jobs/999999/trace").status_code == 404


def test_trace_does_not_leak_between_jobs(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "evaluate", lambda clip, path: jobs.review.judge([], None, "降级"))
    first = _run(session, world, settings, MockProvider(Path("第一集/s1_demo.mp4")), idx=25, key="a")
    second = _run(session, world, settings, MockProvider(Path("第一集/s1_demo.mp4")), idx=26, key="b")
    assert all(row.job_id == first.id for row in _steps(session, first))
    assert all(row.job_id == second.id for row in _steps(session, second))


def test_clip_without_acceptance_is_approved_but_marked_unaudited(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """零验收点 ≠ 判不了：没有可机检项就不该把出片永远堵在 review。

    第二集的终剪文档没有验收点段，实测会一直 `awaiting_human`。这里把口径钉死：
    **出片放行，但 trace 记 `no_criteria`** —— 不假装质检通过。
    """
    monkeypatch.setattr(jobs, "evaluate", lambda clip, path: jobs.review.judge([], None, None))
    clip = _clip(session, world, idx=27)
    clip.acceptance_json = None
    session.flush()
    job, _ = jobs.submit(
        session, user_id=world["owner"].id, clip=clip, budget_limit=Decimal("12.000000"),
        mode="auto", idem_header=f"unaudited-{clip.id}", payload={"clip_id": clip.id},
    )
    session.flush()
    jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4")))

    session.refresh(clip)
    assert clip.status == "approved"
    decision = _steps(session, job)[-1]
    assert decision.step == "decision"
    assert decision.verdict == "no_criteria"
    assert decision.detail["criteria"] == 0


def test_clip_with_unknown_criteria_stays_in_review(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有验收点但判不了 → 仍然停在 review（`unknown` 不算通过的回归防线）。"""
    monkeypatch.setattr(
        jobs, "evaluate",
        lambda clip, path: jobs.review.judge([{"id": "a1", "criterion": "功德环在画"}], None, "降级"),
    )
    clip = _clip(session, world, idx=28)
    clip.acceptance_json = [{"id": "a1", "criterion": "功德环在画"}]
    session.flush()
    job, _ = jobs.submit(
        session, user_id=world["owner"].id, clip=clip, budget_limit=Decimal("12.000000"),
        mode="auto", idem_header=f"unknown-{clip.id}", payload={"clip_id": clip.id},
    )
    session.flush()
    jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4")))

    session.refresh(clip)
    assert clip.status == "review"
    assert _steps(session, job)[-1].verdict == "awaiting_human"
