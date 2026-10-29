"""业务逻辑层对抗：自愈循环的停止条件、预算累积、归因、记忆去重、幂等指纹。

这些断言不是"验证功能存在"，而是**在边界上问"停止条件真的成立吗"**：
- 重试到底调了几次上游、钱有没有在重试间累积到穿线；
- 归因不出来时会不会空转；
- 预算不足时是**调用前**拒还是**花完再**发现；
- 同 scope 同因同修的教训会不会刷成一堆重复行。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from manju import jobs, lessons
from manju.config import Settings
from manju.models import Clip, Job, Lesson, Revision
from manju.providers import MockProvider
from tests.conftest import login

FAST = Path("第一集/s1_demo.mp4")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def media(media_root: Path) -> Path:
    (media_root / "第一集").mkdir(parents=True, exist_ok=True)
    (media_root / "第一集" / "s1_demo.mp4").write_bytes(b"\x00" * 128)
    return media_root


def _clip(session: Session, world: dict, *, idx: int = 20, acceptance: list[dict] | None = None) -> Clip:
    clip = Clip(
        episode_id=world["episode"].id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"),
        status="prompted", prompt="镜头1（0-6秒）：远景急推",
        shots_json=[{"no": 1, "start": 0, "end": 6, "desc": "远景急推"}],
        acceptance_json=acceptance,
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return clip


def _job(session: Session, world: dict, clip: Clip, *, mode: str = "auto", budget: str = "12.000000", key: str = "biz") -> Job:
    job, _ = jobs.submit(
        session, user_id=world["owner"].id, clip=clip, budget_limit=Decimal(budget), mode=mode,
        idem_header=f"{key}-{clip.id}", payload={"clip_id": clip.id, "mode": mode},
    )
    session.flush()
    return job


def _revisions(session: Session, clip: Clip) -> list[Revision]:
    return list(session.scalars(select(Revision).where(Revision.target_id == clip.id).order_by(Revision.id)))


# ---------------------------------------------------------------- 自愈循环


def test_auto_retry_stops_at_max_attempts_and_counts_every_call(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """永远失败 + 归因可执行 → 恰好跑满 max_attempts 次上游调用就转人工，绝不空转。"""
    clip = _clip(session, world)
    job = _job(session, world, clip)
    # 永远失败，且归因**可执行**（evidence 里带"定镜" → attribute 会给出 fix）
    monkeypatch.setattr(
        jobs, "evaluate",
        lambda c, p: jobs.review.Report(
            motion=None, results=[{"id": "a1", "result": "fail", "evidence": "开场定镜空景"}],
            verdict="fail", degraded=False, note=None,
        ),
    )
    for _ in range(6):
        if job.state in ("failed", "done", "cancelled", "paused_budget"):
            break
        jobs.run_pending(session, settings, "t", MockProvider(FAST))
        session.refresh(job)

    assert job.state == "failed"
    assert job.error_code == "ACCEPTANCE_FAIL"
    assert job.attempts == job.max_attempts - 1  # 3 次执行 = attempts 0/1/2
    assert len(_revisions(session, clip)) == job.max_attempts  # 每次尝试都真的调了一次上游
    # 3 次 × 1.135002，钱一次都没漏记
    assert job.cost_used == Decimal("1.135002") * job.max_attempts


def test_unattributable_failure_escalates_without_retry(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """归因不出可执行改动（fix=None）→ 一次就转人工，不重试。"""
    clip = _clip(session, world)
    job = _job(session, world, clip)
    monkeypatch.setattr(
        jobs, "evaluate",
        lambda c, p: jobs.review.Report(motion=None, results=[{"id": "a1", "result": "fail", "evidence": "内容级失败"}],
                                        verdict="fail", degraded=True, note="降级"),
    )
    jobs.run_pending(session, settings, "t", MockProvider(FAST))
    session.refresh(job)
    assert job.state == "failed"
    assert job.error_code == "ESCALATED"
    assert job.attempts == 0
    assert len(_revisions(session, clip)) == 1


def test_budget_accumulates_across_retries(
    session: Session, settings: Settings, world: dict, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次尝试前预算不够（1.5 元 < 1.135×2）→ 停在 paused_budget，不再调上游。"""
    clip = _clip(session, world)
    job = _job(session, world, clip, budget="1.500000")
    monkeypatch.setattr(
        jobs, "evaluate",
        lambda c, p: jobs.review.Report(motion=None, results=[{"id": "a1", "result": "fail", "evidence": "定镜"}],
                                        verdict="fail", degraded=False, note=None),
    )
    monkeypatch.setattr(jobs, "attribute", lambda report: ("acceptance_fail", "开场不要定镜", "定镜"))

    jobs.run_pending(session, settings, "t", MockProvider(FAST))
    session.refresh(job)
    assert job.state == "queued" and job.attempts == 1  # 第一次失败后已重排
    jobs.run_pending(session, settings, "t", MockProvider(FAST))
    session.refresh(job)
    assert job.state == "paused_budget"  # 第二次预算不够，调用前就停
    assert len(_revisions(session, clip)) == 1  # 只花了一次钱


def test_precheck_blocks_call_when_estimate_exceeds_budget(
    session: Session, settings: Settings, world: dict, media: Path
) -> None:
    """预算比单次预估还小 → 一次上游都不调、一条 revision 都不产生。"""
    clip = _clip(session, world)
    job = _job(session, world, clip, budget="0.500000")  # 6 s × 0.189167 = 1.135002
    jobs.run_pending(session, settings, "t", MockProvider(FAST))
    session.refresh(job)
    assert job.state == "paused_budget"
    assert job.cost_used == Decimal("0")
    assert _revisions(session, clip) == []


# ---------------------------------------------------------------- 记忆与幂等


def test_lesson_dedupes_by_symptom_and_counts_hits(session: Session, world: dict) -> None:
    """同 scope 同因同修只留一行，命中次数累加 —— 否则记忆会刷成一堆重复行。"""
    scope = f"project:{world['project'].id}"
    for _ in range(3):
        lessons.remember(
            session, scope=scope, failure_type="acceptance_fail",
            symptom="结尾定格 2 秒", fix="收尾保持位移", evidence_revision_id=None,
        )
    rows = session.scalars(select(Lesson).where(Lesson.scope == scope)).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 3


def test_idempotency_fingerprint_includes_mode(
    client: TestClient, world: dict, settings: Settings
) -> None:
    """同 key 不同 mode 必须 409：指纹不含 mode 的话，两次不同语义的提交会被当成重放。"""
    login(client, world["owner"], settings)
    headers = {"Idempotency-Key": "biz-mode", "If-Match": f'"{world["clip"].version}"'}
    first = client.post(f"/api/v1/clips/{world['clip'].id}/generate?mode=auto", json={"res": "draft"}, headers=headers)
    assert first.status_code == 202
    second = client.post(f"/api/v1/clips/{world['clip'].id}/generate?mode=default", json={"res": "draft"}, headers=headers)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "IDEM_KEY_REUSED"


def test_project_daily_budget_blocks_new_generation(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """`BUDGET_PROJECT_DAILY`（100 元/项目/天）必须是一道**提交前的闸**（`03` §7.3）。

    撞出来的缺口：这个常量以前只有声明和文档，`jobs.submit` 里没有实现 ——
    项目当天花超了照样能提交。
    """
    from manju.constants import BUDGET_PROJECT_DAILY

    clip = _clip(session, world, idx=21)
    session.add(
        Revision(
            target_kind="clip", target_id=clip.id, prompt="p",
            cost=BUDGET_PROJECT_DAILY + Decimal("1"), params_json={}, provider="mock", created_at=_now(),
        )
    )
    session.flush()
    spent = session.scalar(
        select(func.coalesce(func.sum(Revision.cost), 0)).where(Revision.target_id == clip.id)
    )
    assert spent > BUDGET_PROJECT_DAILY

    login(client, world["owner"], settings)
    response = client.post(
        f"/api/v1/clips/{clip.id}/generate?mode=auto",
        json={"res": "draft", "budget_limit": "12.000000"},
        headers={"Idempotency-Key": "biz-daily", "If-Match": f'"{clip.version}"'},
    )
    assert response.status_code == 402, response.text
    error = response.json()["error"]
    assert error["code"] == "BUDGET_EXCEEDED"
    assert error["detail"]["scope"] == "project_daily"


def test_project_daily_budget_allows_under_limit(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """没到日上限时照常放行（闸不能误伤正常提交）。"""
    clip = _clip(session, world, idx=22)
    session.add(
        Revision(
            target_kind="clip", target_id=clip.id, prompt="p", cost=Decimal("1.000000"),
            params_json={}, provider="mock", created_at=_now(),
        )
    )
    session.flush()
    login(client, world["owner"], settings)
    response = client.post(
        f"/api/v1/clips/{clip.id}/generate?mode=auto",
        json={"res": "draft", "budget_limit": "12.000000"},
        headers={"Idempotency-Key": "biz-daily-ok", "If-Match": f'"{clip.version}"'},
    )
    assert response.status_code == 202, response.text
