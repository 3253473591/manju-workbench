"""动线质检、验收判定、归因重试（`03` §4 / `docs/video-motion-review/SKILL.md`）。

`vscan` 的解析用**真实输出片段**当夹具，不真跑 PowerShell；自动重试把 `jobs.evaluate`
换成脚本化的判定，才能稳定测"连续两次同因同修就停"这类边界。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import jobs, lessons, review
from manju.config import Settings
from manju.models import Clip, Job, Lesson, Revision
from manju.providers import MockProvider
from manju.review import Motion, Report
from tests.conftest import login

VSCAN_SAMPLE = """file    : D:\\clips\\s1.mp4
clip    : 12.096 s, 1280x720, h264
scan    : 23 samples at 2 fps in 0.22 s (x56 realtime)

motion timeline (mean pixel change per sample; # = relative motion):
  0.50s |####################                |  26.98
  1.00s |#####################               |  28.55
  1.50s |#########################           |  35.04
  2.00s |##########################          |  35.44

stats   : min=7.76  avg=28  max=49.63  sd=10.43

ACTION  : 2 flagged moment(s) -- inspect these timecodes first:
  t=   4.00s  motion= 49.35  [burst]
  t=   8.50s  motion= 49.63  [burst]
cost    : scan took 0.42 s
"""

VSCAN_FREEZE = VSCAN_SAMPLE.replace(
    "  t=   4.00s  motion= 49.35  [burst]",
    "  t=   4.00s  motion=  0.30  [FREEZE]",
)


def _motion(**overrides) -> Motion:
    base = dict(
        samples=23,
        duration_s=Decimal("12.00"),
        avg=28.0,
        peak=49.63,
        minimum=7.76,
        freeze=0,
        tail_collapse=False,
        flagged=[],
        series=[26.98, 28.55, 35.04, 35.44],
    )
    base.update(overrides)
    return Motion(**base)


def test_parse_real_vscan_output() -> None:
    motion, why = review.parse(VSCAN_SAMPLE)
    assert why is None
    assert motion is not None
    assert (motion.samples, motion.freeze, motion.tail_collapse) == (23, 0, False)
    assert motion.avg == 28.0 and motion.peak == 49.63
    assert len(motion.flagged) == 2
    assert len(motion.series) == 4
    assert motion.duration_s == Decimal("12.096")


def test_parse_missing_stats_is_an_error() -> None:
    motion, why = review.parse("file: x.mp4\nnothing here\n")
    assert motion is None
    assert "stats" in (why or "")


def test_scan_degrades_without_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VSCAN_PATH", str(tmp_path / "missing.ps1"))
    motion, why = review.scan(tmp_path / "clip.mp4")
    assert motion is None
    assert "vscan" in (why or "")


def test_judge_opening_freeze_and_content() -> None:
    acceptance = [
        {"id": "a1", "criterion": "开场禁空景"},
        {"id": "a2", "criterion": "末段定格"},
        {"id": "a3", "criterion": "功德环全程在画"},
    ]
    ok = review.judge(acceptance, _motion(), None)
    assert ok.verdict == "pass"
    assert [item["result"] for item in ok.results] == ["pass", "pass", "unknown"]

    frozen = review.judge(acceptance, _motion(freeze=1, tail_collapse=True), None)
    assert frozen.verdict == "fail"

    static = review.judge([{"id": "a1", "criterion": "开场定镜"}], _motion(series=[0.2, 0.1, 0.3]), None)
    assert static.results[0]["result"] == "fail"
    assert "定镜" in static.results[0]["evidence"]


def test_judge_without_motion_is_unknown_not_pass() -> None:
    report = review.judge([{"id": "a1", "criterion": "开场禁空景"}], None, "降级")
    assert report.verdict == "unknown"
    assert report.degraded is True
    assert report.results[0]["result"] == "unknown"


def test_judge_with_no_judgeable_criteria_stays_unknown() -> None:
    report = review.judge([{"id": "a1", "criterion": "功德环全程在画"}], _motion(), None)
    assert report.verdict == "unknown"


# ---------------------------------------------------------------- 归因与记忆


def test_attribute_maps_freeze_to_actionable_fix() -> None:
    report = review.judge(
        [{"id": "a2", "criterion": "末段定格"}], _motion(freeze=2), None
    )
    failure_type, fix, symptom = jobs.attribute(report)
    assert failure_type == "motion_freeze"
    assert fix and "静止" in fix
    assert "a2" in symptom


def test_attribute_without_motion_is_not_actionable() -> None:
    report = review.judge([{"id": "a1", "criterion": "开场禁空景"}], None, "降级")
    _, fix, _ = jobs.attribute(report)
    assert fix is None


def test_lesson_dedupes_by_symptom(session: Session, world: dict) -> None:
    first = lessons.remember(
        session, scope="project:1", failure_type="motion_freeze", symptom="a2 冻结 1 处", fix="f1"
    )
    second = lessons.remember(
        session, scope="project:1", failure_type="motion_freeze", symptom="a2  冻结 1 处", fix="f2"
    )
    assert first.id == second.id
    assert second.hit_count == 2
    assert second.fix == "f2"
    assert len(lessons.recall(session, "project:1")) == 1


# ---------------------------------------------------------------- 自动重试


def _conflict(session: Session, world: dict) -> Clip:
    clip = Clip(
        episode_id=world["episode"].id,
        idx=7,
        source_idx=7,
        duration_s=Decimal("6.00"),
        status="prompted",
        prompt="镜头1（0-6秒）：远景急推",
        acceptance_json=[{"id": "a2", "criterion": "末段定格"}],
        created_at=world["clip"].created_at,
        updated_at=world["clip"].updated_at,
    )
    session.add(clip)
    session.flush()
    return clip


def _submit(session: Session, clip: Clip, mode: str = "auto") -> Job:
    job, _ = jobs.submit(
        session,
        user_id=1,
        clip=clip,
        budget_limit=Decimal("12.00"),
        mode=mode,
        idem_header=f"t-{clip.id}-{mode}",
        payload={"clip_id": clip.id, "mode": mode},
    )
    session.flush()
    return job


def test_auto_mode_rewrites_prompt_and_retries(
    session: Session, settings: Settings, world: dict, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    clip = _conflict(session, world)
    job = _submit(session, clip, "auto")

    outcomes = iter(
        [
            review.judge([{"id": "a2", "criterion": "末段定格"}], _motion(freeze=1), None),
            review.judge([{"id": "a2", "criterion": "末段定格"}], _motion(), None),
        ]
    )
    monkeypatch.setattr(jobs, "evaluate", lambda clip, path: next(outcomes))

    assert jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4"))) == 1
    session.refresh(job)
    assert job.state == "queued" and job.attempts == 1
    assert job.error_code == "ACCEPTANCE_FAIL"
    assert "静止" in (clip.prompt or "")
    stored = session.scalars(select(Lesson).where(Lesson.failure_type == "motion_freeze")).all()
    assert len(stored) == 1 and stored[0].hit_count == 1

    assert jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4"))) == 1
    session.refresh(job)
    assert job.state == "done"
    session.refresh(clip)
    assert clip.status == "approved"
    # 6 s 片段 × 0.189167 元/秒 = 1.135002 元/次；两次尝试都要记在账上\n    assert job.cost_used == Decimal("1.135002") * 2


def test_auto_mode_stops_at_max_attempts(
    session: Session, settings: Settings, world: dict, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    clip = _conflict(session, world)
    job = _submit(session, clip, "auto")
    monkeypatch.setattr(
        jobs, "evaluate", lambda clip, path: review.judge([{"id": "a2", "criterion": "末段定格"}], _motion(freeze=1), None)
    )

    for _ in range(4):
        if job.state == "failed":
            break
        jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4")))

    session.refresh(job)
    assert job.state == "failed"
    assert job.error_code == "ACCEPTANCE_FAIL"
    assert job.attempts == job.max_attempts - 1  # 用尽后不再自增


def test_default_mode_fails_without_retry(
    session: Session, settings: Settings, world: dict, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认模式要停在付费点等人，不会自己改词重发。"""
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    clip = _conflict(session, world)
    job = _submit(session, clip, "default")
    jobs.confirm(session, job)
    monkeypatch.setattr(
        jobs, "evaluate", lambda clip, path: review.judge([{"id": "a2", "criterion": "末段定格"}], _motion(freeze=1), None)
    )
    jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4")))
    session.refresh(job)
    assert job.state == "failed"
    assert session.scalars(select(Lesson)).all() == []


def test_revision_records_motion_and_results(
    session: Session, settings: Settings, world: dict, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    clip = _conflict(session, world)
    job = _submit(session, clip, "default")
    jobs.confirm(session, job)
    monkeypatch.setattr(
        jobs, "evaluate", lambda clip, path: review.judge([{"id": "a2", "criterion": "末段定格"}], _motion(), None)
    )
    jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4")))
    revision = session.scalars(select(Revision).where(Revision.target_id == clip.id)).one()
    assert revision.motion_json["freeze"] == 0
    assert revision.acceptance_result_json[0]["result"] == "pass"
    session.refresh(clip)
    assert clip.status == "approved"
