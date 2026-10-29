"""Golden 回归集（20 条）：改 Skill / 模板 / 状态机之后，跑这一套判断"有没有把别的弄坏"。

它不是覆盖率测试，而是**契约测试**：每条只断言"这个契约必须成立"，
不依赖模型措辞（所以 mock provider 也能跑，零成本、可重复）。
想扩就往下加，编号连续即可（`README` 里有怎么加）。

分组：
A. LLM 五环节（5）—— 一条代码路径，五个 stage 的写回与状态推进
B. 域不变量（7）—— `01` §5.4 的规则必须拦住坏数据
C. 协议与边界（8）—— 幂等、If-Match、预算、状态机、维护操作
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import clip_rules, jobs, steps
from manju.config import Settings
from manju.constants import TRACE_KEEP
from manju.errors import ApiError, ErrorCode
from manju.models import Clip, Episode, Job, JobTrace, Project
from manju.providers import MockProvider
from tests.conftest import login

FAST = Path("第一集/s1_demo.mp4")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _clip(session: Session, world: dict, *, idx: int = 12, **extra) -> Clip:
    fields = {
        "episode_id": world["episode"].id, "idx": idx, "source_idx": idx, "duration_s": Decimal("6.00"),
        "status": "draft", "created_at": _now(), "updated_at": _now(),
    }
    fields.update(extra)
    clip = Clip(**fields)
    session.add(clip)
    session.flush()
    return clip


def _episode(session: Session, project_id: int, idx: int) -> Episode:
    episode = Episode(
        project_id=project_id, idx=idx, title=f"第{idx}集", status="draft", version=1,
        created_at=_now(), updated_at=_now(),
    )
    session.add(episode)
    session.flush()
    return episode


# ---------------------------------------------------------------- A. LLM 五环节


def test_01_s1_writes_logline_and_genre(
    client: TestClient, world: dict, settings: Settings
) -> None:
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/projects/{world['project'].id}/steps/S1/run")
    assert response.status_code == 200
    assert "project.logline" in response.json()["written"]


def test_02_s2_writes_synopsis_and_advances_episode(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    episode = _episode(session, world["project"].id, 4)
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/episodes/{episode.id}/steps/S2/run")
    assert response.status_code == 200
    session.refresh(episode)
    assert episode.synopsis and episode.status == "scripted"


def test_03_s3_creates_clips_and_marks_storyboarded(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    episode = _episode(session, world["project"].id, 5)
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/episodes/{episode.id}/steps/S3/run")
    assert response.status_code == 200
    session.refresh(episode)
    assert episode.status == "storyboarded"
    assert session.scalars(select(Clip).where(Clip.episode_id == episode.id)).all()


def test_04_s4_writes_prompt_only_with_dialogue_embedded(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """S4 的产物必须过"台词逐字内嵌"：上下文里给了台词，模型才有得嵌。"""
    clip = _clip(
        session, world, idx=13, status="prompted",
        shots_json=[{"no": 1, "start": 0, "end": 6, "desc": "远景急推"}],
        dialogue_json=[{"speaker": "徒弟", "text": "退钱", "shot_no": 1, "on_screen": True}],
    )
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/clips/{clip.id}/steps/S4/run")
    assert response.status_code == 200, response.text
    assert "退钱" in response.json()["payload"]["prompt"]


def test_05_s5_produces_characters_without_writing_back(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    project = session.get(Project, world["project"].id)
    before = (project.logline, project.genre, project.tone)
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/projects/{project.id}/steps/S5/run")
    assert response.status_code == 200
    assert response.json()["payload"]["characters"]
    session.refresh(project)
    assert (project.logline, project.genre, project.tone) == before  # 只产出不落库


# ---------------------------------------------------------------- B. 域不变量


def test_06_shots_must_close_on_duration() -> None:
    problems = clip_rules.domain.shots_problems(
        [{"no": 1, "start": 0, "end": 5}], Decimal("6.00")
    )
    assert problems and "末镜" in problems[0]


def test_07_shot_numbers_must_be_contiguous() -> None:
    problems = clip_rules.domain.shots_problems(
        [{"no": 1, "start": 0, "end": 6}, {"no": 3, "start": 6, "end": 12}], Decimal("12.00")
    )
    assert problems and "连续" in problems[0]


def test_08_dialogue_must_be_embedded_in_prompt() -> None:
    problems = clip_rules.domain.dialogue_problems(
        [{"speaker": "徒弟", "text": "退钱"}], [{"no": 1}], "镜头1：远景急推"
    )
    assert problems and "没有内嵌" in problems[0]


def test_09_acceptance_ids_must_be_unique() -> None:
    problems = clip_rules.domain.acceptance_problems(
        [{"id": "a1", "criterion": "x"}, {"id": "a1", "criterion": "y"}]
    )
    assert problems and "重复" in problems[0]


def test_10_slot_indices_must_be_contiguous() -> None:
    problems = clip_rules.domain.slots_problems(
        [{"idx": 2, "kind": "image", "asset_ref": "a"}, {"idx": 3, "kind": "image", "asset_ref": "b"}]
    )
    assert problems and "连续" in problems[0]


def test_11_cost_must_match_unit_price_times_quantity() -> None:
    problems = clip_rules.domain.cost_problems(
        Decimal("9.99"), Decimal("0.189167"), Decimal("12.00"), None
    )
    assert problems and "不一致" in problems[0]


def test_12_illegal_state_transition_is_rejected() -> None:
    with pytest.raises(ApiError) as caught:
        clip_rules.enforce_transition("draft", "approved")
    assert caught.value.code == ErrorCode.JOB_STATE_INVALID


# ---------------------------------------------------------------- C. 协议与边界


def test_13_zero_budget_is_rejected_not_defaulted(
    client: TestClient, world: dict, settings: Settings
) -> None:
    login(client, world["owner"], settings)
    response = client.post(
        f"/api/v1/clips/{world['clip'].id}/generate",
        json={"res": "draft", "budget_limit": "0"},
        headers={"Idempotency-Key": "golden-budget", "If-Match": f'"{world["clip"].version}"'},
    )
    assert response.status_code == 422


def test_14_stale_if_match_is_412(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    response = client.patch(
        f"/api/v1/clips/{world['clip'].id}", json={"title": "新标题"}, headers={"If-Match": '"999"'}
    )
    assert response.status_code == 412


def test_15_idempotent_replay_returns_200(
    client: TestClient, world: dict, settings: Settings
) -> None:
    login(client, world["owner"], settings)
    headers = {"Idempotency-Key": "golden-replay", "If-Match": f'"{world["clip"].version}"'}
    first = client.post(f"/api/v1/clips/{world['clip'].id}/generate?mode=auto", json={"res": "draft"}, headers=headers)
    second = client.post(f"/api/v1/clips/{world['clip'].id}/generate?mode=auto", json={"res": "draft"}, headers=headers)
    assert first.status_code == 202 and second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"


def test_16_reroll_requires_rejected_state(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    clip = _clip(session, world, idx=14, status="review", prompt="p")
    login(client, world["owner"], settings)
    response = client.post(
        f"/api/v1/clips/{clip.id}/reroll",
        json={"res": "draft"},
        headers={"Idempotency-Key": "golden-reroll", "If-Match": f'"{clip.version}"'},
    )
    assert response.status_code == 409


def test_17_publish_blocked_without_dry_run(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    from manju import skills as skill_lib
    from manju.models import Skill

    skill = Skill(name="golden-skill", stage="S4", status="active", created_at=_now())
    session.add(skill)
    session.flush()
    version, _ = skill_lib.create_version(
        session, skill=skill, body_md="## #110 巨物\n正文", created_by=world["admin"].id
    )
    session.commit()
    login(client, world["admin"], settings)
    response = client.post(f"/api/v1/skills/{skill.id}/versions/{version.version}/publish")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SKILL_NOT_DRYRUN"


def test_18_clip_rollback_is_idempotent(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    from manju.models import Revision

    clip = world["clip"]
    revision = Revision(
        target_kind="clip", target_id=clip.id, prompt="旧提示词", cost=Decimal(0),
        params_json={}, provider="mock", created_at=_now(),
    )
    session.add(revision)
    session.flush()
    clip.current_revision_id = revision.id
    session.flush()
    version = clip.version
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/clips/{clip.id}/rollback", json={"revision_id": revision.id})
    assert response.status_code == 200 and response.json()["version"] == version


def test_19_episode_move_to_tail(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    second = _episode(session, world["project"].id, 2)
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/episodes/{second.id}/move", json={"to_idx": 50})
    assert response.status_code == 200
    assert response.json()[-1]["id"] == second.id


def test_20_job_trace_is_capped_at_trace_keep(
    session: Session, settings: Settings, world: dict
) -> None:
    clip = _clip(session, world, idx=15, status="prompted", prompt="p",
                 shots_json=[{"no": 1, "start": 0, "end": 6, "desc": "x"}])
    job = Job(
        kind="video", target_kind="clip", target_id=clip.id, state="running", attempts=0,
        max_attempts=3, budget_limit=Decimal("12.000000"), cost_used=Decimal(0),
        idem_key="g" * 64, mode="default", created_at=_now(), locked_by="t#1",
    )
    session.add(job)
    session.flush()
    for index in range(TRACE_KEEP + 10):
        jobs.record_trace(session, job, f"s{index}")
    kept = session.scalars(select(JobTrace).where(JobTrace.job_id == job.id)).all()
    assert len(kept) == TRACE_KEEP
