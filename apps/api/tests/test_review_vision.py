"""内容级验收点的视觉判定（`03` §6.3：判不了写 unknown，判得了才落地）。

三件事各钉一条：动线判不了的才交视觉（不重复判）、视觉判不了仍然是 unknown（不猜）、
抽帧降级不阻断链路（缺 ffmpeg 时链路照跑）。MockProvider 的判定是预设表，evidence 带
`[mock]`，测试同时钉住这个标记 —— 它是对外说明"这不是模型判的"的唯一凭据。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import jobs, review
from manju.config import Settings
from manju.models import Clip, Job, Revision
from manju.providers import MockProvider
from manju.review import Motion, Report


def _motion(**overrides) -> Motion:
    base = dict(
        samples=23,
        duration_s=None,
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


def _report(*results: dict) -> Report:
    return Report(motion=_motion(), results=list(results), verdict=review._verdict(list(results)), degraded=False)


@pytest.fixture
def sheet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """跳过真抽帧：这一层测的是"判定怎么合并"，抽帧本身由 ffmpeg 负责。"""
    image = tmp_path / "sheet-ab12cd34ef56.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(review, "contact_sheet", lambda media_path, out_dir: (image, None))
    return image


def test_contact_sheet_degrades_without_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review.shutil, "which", lambda name: None)
    path, note = review.contact_sheet(tmp_path / "s1.mp4", tmp_path)
    assert path is None
    assert note is not None and "ffmpeg" in note


def test_contact_sheet_degrades_without_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review.shutil, "which", lambda name: "C:/ffmpeg/bin/ffmpeg.exe")
    monkeypatch.setattr(review, "probe_duration", lambda media_path: None)
    path, note = review.contact_sheet(tmp_path / "s1.mp4", tmp_path)
    assert path is None
    assert note is not None and "时长" in note


def test_vision_fills_content_criteria(sheet: Path, tmp_path: Path) -> None:
    report = _report(
        {"id": "a1", "result": "pass", "evidence": "开场动线 26.98"},
        {"id": "a2", "result": "unknown", "evidence": "内容级判据，动线判不了（需视觉模型）"},
    )
    acceptance = [{"id": "a1", "criterion": "开场不是定镜"}, {"id": "a2", "criterion": "功德环全程在画"}]

    judged = review.judge_with_vision(
        acceptance, report, tmp_path / "s1.mp4", MockProvider(fixture=None), workdir=tmp_path
    )

    assert judged.results[0] == report.results[0]  # 动线判过的保持原样
    assert judged.results[1]["result"] == "pass"
    assert judged.results[1]["evidence"].startswith("[mock]")
    assert judged.verdict == "pass"
    assert judged.note is not None and "1/1 条判出" in judged.note


def test_vision_keeps_unknown_without_preset(sheet: Path, tmp_path: Path) -> None:
    report = _report({"id": "a3", "result": "unknown", "evidence": "内容级判据"})
    acceptance = [{"id": "a3", "criterion": "背景必须是清晨的雾"}]

    judged = review.judge_with_vision(
        acceptance, report, tmp_path / "s1.mp4", MockProvider(fixture=None), workdir=tmp_path
    )

    assert judged.results[0]["result"] == "unknown"
    assert judged.verdict == "unknown"  # 判不了 ≠ 通过


def test_vision_does_not_override_motion_failure(sheet: Path, tmp_path: Path) -> None:
    report = _report(
        {"id": "a2", "result": "fail", "evidence": "冻结 3 处"},
        {"id": "a4", "result": "unknown", "evidence": "内容级判据"},
    )
    acceptance = [{"id": "a2", "criterion": "全程无冻结"}, {"id": "a4", "criterion": "功德环全程在画"}]

    judged = review.judge_with_vision(
        acceptance, report, tmp_path / "s1.mp4", MockProvider(fixture=None), workdir=tmp_path
    )

    assert judged.results[0]["result"] == "fail"
    assert judged.verdict == "fail"


def test_vision_flagged_subtitle_fails(sheet: Path, tmp_path: Path) -> None:
    report = _report({"id": "a5", "result": "unknown", "evidence": "内容级判据"})
    acceptance = [{"id": "a5", "criterion": "画面无字幕"}]

    judged = review.judge_with_vision(
        acceptance, report, tmp_path / "s1.mp4", MockProvider(fixture=None), workdir=tmp_path
    )

    assert judged.results[0]["result"] == "fail"
    assert "烧入文字" in judged.results[0]["evidence"]


def test_vision_skipped_when_nothing_pending(sheet: Path, tmp_path: Path) -> None:
    report = _report({"id": "a1", "result": "pass", "evidence": "开场动线 26.98"})
    judged = review.judge_with_vision(
        [{"id": "a1", "criterion": "开场不是定镜"}],
        report,
        tmp_path / "s1.mp4",
        MockProvider(fixture=None),
        workdir=tmp_path,
    )
    assert judged is report  # 没有待判条目就不抽帧、不问模型


def test_job_judges_content_criteria_with_vision(
    session: Session, settings: Settings, world: dict, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """链路级：动线拿不到 → 内容判据 unknown → 视觉判 pass → 片段 approved。

    这正是 P0-6 要讲的那件事：动线是分诊信号，判决权在内容层。
    """
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    clip = Clip(
        episode_id=world["episode"].id,
        idx=8,
        source_idx=8,
        duration_s=Decimal("6.00"),
        status="prompted",
        prompt="镜头1（0-6秒）：远景急推",
        acceptance_json=[{"id": "a1", "criterion": "功德环全程在画"}],
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(clip)
    session.flush()
    job, _ = jobs.submit(
        session,
        user_id=world["owner"].id,
        clip=clip,
        budget_limit=Decimal("12.00"),
        mode="default",
        idem_header=f"vision-{clip.id}",
        payload={"clip_id": clip.id},
    )
    session.flush()
    jobs.confirm(session, job)

    sheet = media_root / "sheet-test.jpg"
    sheet.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(review, "scan", lambda path: (None, "未找到 vscan.ps1"))
    monkeypatch.setattr(review, "contact_sheet", lambda media, out_dir: (sheet, None))

    jobs.run_pending(session, settings, "t", MockProvider(Path("第一集/s1_demo.mp4")))

    revision = session.scalars(select(Revision).where(Revision.target_id == clip.id)).one()
    assert revision.acceptance_result_json[0]["result"] == "pass"
    assert revision.acceptance_result_json[0]["evidence"].startswith("[mock]")
    session.refresh(clip)
    assert clip.status == "approved"


def test_vision_off_keeps_unknown(
    session: Session, settings: Settings, world: dict, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`VISION=0` 时回到老口径：判不了就是 unknown，片段留在 review 等人看。"""
    from dataclasses import replace as dataclass_replace

    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    clip = Clip(
        episode_id=world["episode"].id,
        idx=9,
        source_idx=9,
        duration_s=Decimal("6.00"),
        status="prompted",
        prompt="镜头1（0-6秒）：远景急推",
        acceptance_json=[{"id": "a1", "criterion": "功德环全程在画"}],
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(clip)
    session.flush()
    job, _ = jobs.submit(
        session,
        user_id=world["owner"].id,
        clip=clip,
        budget_limit=Decimal("12.00"),
        mode="default",
        idem_header=f"vision-off-{clip.id}",
        payload={"clip_id": clip.id},
    )
    session.flush()
    jobs.confirm(session, job)
    monkeypatch.setattr(review, "scan", lambda path: (None, "未找到 vscan.ps1"))

    jobs.run_pending(session, dataclass_replace(settings, vision=False), "t", MockProvider(Path("第一集/s1_demo.mp4")))

    revision = session.scalars(select(Revision).where(Revision.target_id == clip.id)).one()
    assert revision.acceptance_result_json[0]["result"] == "unknown"
    session.refresh(clip)
    assert clip.status == "review"
