"""数值化质检门：判据都 pass 但均分偏低时，默认留人、auto 允许再试一次。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import jobs, review
from manju.config import Settings
from manju.models import Clip, Job, JobTrace, Lesson
from manju.providers import MockProvider, _clean_verdicts
from manju.review import Report

FAST = Path("第一集/s1_demo.mp4")
EDGE = [{"id": "a1", "result": "pass", "score": 40, "evidence": "构图边缘"}]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def media(media_root: Path) -> Path:
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    return media_root


def _clip(session: Session, world: dict, *, idx: int = 24) -> Clip:
    clip = Clip(
        episode_id=world["episode"].id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"),
        status="prompted", prompt="镜头1（0-6秒）：远景急推",
        shots_json=[{"no": 1, "start": 0, "end": 6, "desc": "远景急推"}],
        acceptance_json=[{"id": "a1", "criterion": "画面有推进"}],
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return clip


def _job(session: Session, world: dict, clip: Clip, *, mode: str, key: str) -> Job:
    job, _ = jobs.submit(
        session, user_id=world["owner"].id, clip=clip, budget_limit=Decimal("12.000000"), mode=mode,
        idem_header=f"{key}-{clip.id}", payload={"clip_id": clip.id, "mode": mode},
    )
    session.flush()
    if job.state == "waiting_confirm":  # 默认模式停在付费点：测试走真实的确认路径
        jobs.confirm(session, job)
    return job


def _decisions(session: Session, job: Job) -> list[JobTrace]:
    return list(
        session.scalars(select(JobTrace).where(JobTrace.job_id == job.id, JobTrace.step == "decision").order_by(JobTrace.id))
    )


def test_clean_verdicts_clamps_score_and_drops_it_for_unknown() -> None:
    cleaned = _clean_verdicts(
        {
            "verdicts": [
                {"id": "a1", "result": "pass", "score": 120, "evidence": "过曝"},
                {"id": "a2", "result": "fail", "score": -5, "evidence": "定镜"},
                {"id": "a3", "result": "unknown", "score": 88, "evidence": "看不清"},
                {"id": "a4", "result": "pass", "score": "88", "evidence": "字符串分数"},
                {"id": "a5", "result": "pass", "score": True, "evidence": "布尔分数"},
            ]
        }
    )
    assert cleaned[0]["score"] == 100
    assert cleaned[1]["score"] == 0
    assert "score" not in cleaned[2], "unknown 不该带分"
    assert "score" not in cleaned[3], "字符串分数不该被接受"
    assert "score" not in cleaned[4], "bool 不该被当成 int"


def test_score_helpers() -> None:
    assert review.score_of([]) is None
    assert review.score_of([{"result": "unknown", "evidence": "x"}]) is None
    assert review.score_of([{"score": 90}, {"score": 60}]) == 75
    assert review.is_low_score([{"score": 60}]) is True
    assert review.is_low_score([{"score": 70}]) is False


def test_judge_motion_pass_gets_coarse_score() -> None:
    motion = review.Motion(samples=23, duration_s=Decimal("6.0"), avg=29.8, peak=51.2, minimum=7.7, freeze=0, tail_collapse=False)
    report = review.judge([{"id": "a1", "criterion": "不要冻结"}], motion, None)
    assert report.results[0]["result"] == "pass"
    assert report.results[0]["score"] == review.MOTION_SCORE_PASS


def test_low_score_default_mode_waits_for_human(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = _clip(session, world)
    job = _job(session, world, clip, mode="default", key="low-default")
    monkeypatch.setattr(
        jobs, "evaluate", lambda c, p: Report(motion=None, results=EDGE, verdict="pass", degraded=False, note=None)
    )
    jobs.run_pending(session, settings, "t", MockProvider(FAST))
    session.refresh(job)
    session.refresh(clip)

    assert job.state == "done"
    assert clip.status == "review", "低分不应该被自动放行"
    decisions = _decisions(session, job)
    assert decisions[-1].verdict == "low_score"
    assert decisions[-1].detail["score"] == 40
    assert decisions[-1].detail["threshold"] == 70
    review_trace = session.scalars(
        select(JobTrace).where(JobTrace.job_id == job.id, JobTrace.step == "review").order_by(JobTrace.id)
    ).all()
    assert review_trace[-1].detail["score"] == 40


def test_low_score_auto_mode_retries_with_fix(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = _clip(session, world, idx=25)
    job = _job(session, world, clip, mode="auto", key="low-auto")
    monkeypatch.setattr(
        jobs, "evaluate", lambda c, p: Report(motion=None, results=EDGE, verdict="pass", degraded=False, note=None)
    )
    jobs.run_pending(session, settings, "t", MockProvider(FAST))
    session.refresh(job)
    session.refresh(clip)

    assert job.state == "queued"
    assert job.attempts == 1
    assert "质检均分偏低" in (clip.prompt or "")
    decisions = _decisions(session, job)
    assert decisions[-1].verdict == "retry"
    assert decisions[-1].detail["failure_type"] == "low_score"
    lessons = session.scalars(select(Lesson).where(Lesson.failure_type == "low_score")).all()
    assert lessons, "低分重试要写进教训库"


def test_high_score_still_approves(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = _clip(session, world, idx=26)
    job = _job(session, world, clip, mode="default", key="high")
    monkeypatch.setattr(
        jobs,
        "evaluate",
        lambda c, p: Report(
            motion=None,
            results=[{"id": "a1", "result": "pass", "score": 88, "evidence": "构图稳"}],
            verdict="pass",
            degraded=False,
            note=None,
        ),
    )
    jobs.run_pending(session, settings, "t", MockProvider(FAST))
    session.refresh(job)
    session.refresh(clip)
    assert clip.status == "approved"
    assert _decisions(session, job)[-1].verdict == "approved"
