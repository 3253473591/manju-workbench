"""Skill 存储与 LLMStep（`02` §3.4 / §3.7，`01` §4.9）。

钉三件事：Skill 切节与幂等版本、step 的上下文组装（预算 + 命中节）、S3 产出的分镜要过
domain 六条不变量。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import skills, steps
from manju.config import Settings
from manju.models import Clip, Episode, Project, Revision, Skill, SkillVersion, User
from manju.providers import MockProvider
from tests.conftest import login

SCRIPT = """# 分场剧本

## 1. 制作规范
- 画幅：16:9 横屏
- 单 Clip ≤ 15 s，镜头时序必须闭合
- 运镜写清楚位移方向

## 2. 角色与资产
- 徒弟：玄黑魔袍、亮蓝手套、脑后金色功德环
"""


# ---------------------------------------------------------------- 切节


def test_split_sections_extracts_rule_ids() -> None:
    body = "## #110 巨物压迫感四件套\n开场要写巨物。\n\n## §2.1 机位位移\n运镜要写背景相对位移。\n"
    sections = skills.split_sections(body)
    assert [section["rule_id"] for section in sections] == ["#110", "§2.1"]
    assert "巨物" in sections[0]["tags"]
    assert "运镜" in sections[1]["tags"]
    assert all(section["tokens_est"] > 0 for section in sections)


def test_upsert_skill_is_idempotent(session: Session, world: dict) -> None:
    first, created = skills.upsert_skill(
        session, name="t-skill", stage="S3", body_md=SCRIPT, created_by=world["owner"].id
    )
    again, created_again = skills.upsert_skill(
        session, name="t-skill", stage="S3", body_md=SCRIPT, created_by=world["owner"].id
    )
    assert created and not created_again
    assert first.id == again.id

    changed, created_new = skills.upsert_skill(
        session, name="t-skill", stage="S3", body_md=SCRIPT + "\n## 3. 补充\n新规则。\n",
        created_by=world["owner"].id,
    )
    assert created_new and changed.version == first.version + 1
    session.refresh(first)
    assert first.state == "superseded"  # 旧版自动让位，`uk_sv_active` 保证只一个 active


def test_select_sections_respects_budget() -> None:
    sections = [
        {"rule_id": "#1", "title": "a", "tags": ["运镜"], "text": "x" * 200, "tokens_est": 100},
        {"rule_id": "#2", "title": "b", "tags": ["台词"], "text": "y" * 400, "tokens_est": 200},
    ]

    class Version:
        sections_json = sections

    chosen, trimmed = skills.select_sections([Version()], budget=150, tags={"运镜"})
    assert [section["rule_id"] for section in chosen] == ["#1"]
    assert trimmed is True


# ---------------------------------------------------------------- LLMStep


@pytest.fixture
def wired(session: Session, world: dict) -> dict:
    skills.upsert_skill(
        session, name="story-to-script", stage="S3", body_md=SCRIPT, created_by=world["owner"].id
    )
    return world


def test_step_templates_expose_stage_and_skill(
    client: TestClient, wired: dict, settings: Settings
) -> None:
    login(client, wired["owner"], settings)
    rows = client.get("/api/v1/steps/templates").json()
    assert [row["stage"] for row in rows] == ["S1", "S2", "S3", "S4", "S5"]
    s3 = next(row for row in rows if row["stage"] == "S3")
    assert s3["skill"] == "story-to-script"
    assert s3["sections"] >= 2


def test_s1_writes_project_fields(
    client: TestClient, wired: dict, settings: Settings, session: Session
) -> None:
    login(client, wired["owner"], settings)
    project_id = wired["project"].id
    before = session.get(Project, project_id).version
    response = client.post(f"/api/v1/projects/{project_id}/steps/S1/run")
    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "S1"
    assert "project.logline" in body["written"]
    assert body["payload"]["logline"]
    session.expire_all()
    project = session.get(Project, project_id)
    assert project.version == before + 1
    assert project.logline == body["payload"]["logline"]

    revision = session.get(Revision, body["revision_id"])
    assert revision.target_kind == "project"
    assert revision.params_json["stage"] == "S1"


def test_s3_creates_clips_passing_invariants(
    client: TestClient, wired: dict, settings: Settings, session: Session
) -> None:
    """S3 产出的分镜必须过 `01` §5.4：时序闭合、台词内嵌、验收点 id 唯一。"""
    empty = Episode(
        project_id=wired["project"].id,
        idx=9,
        title="第九集",
        status="draft",
        version=1,
        created_at=wired["clip"].created_at,
        updated_at=wired["clip"].updated_at,
    )
    session.add(empty)
    session.flush()
    login(client, wired["owner"], settings)

    response = client.post(f"/api/v1/episodes/{empty.id}/steps/S3/run")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["written"] == ["episode.clips×1"]

    clips = session.scalars(select(Clip).where(Clip.episode_id == empty.id)).all()
    assert len(clips) == 1
    assert clips[0].title
    assert clips[0].shots_json[0]["no"] == 1
    assert clips[0].status == "draft"


def test_s3_refuses_when_episode_has_clips(
    client: TestClient, wired: dict, settings: Settings
) -> None:
    login(client, wired["owner"], settings)
    response = client.post(f"/api/v1/episodes/{wired['episode'].id}/steps/S3/run")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT_CLIP_INDEX"


def test_s4_writes_prompt_and_advances_status(
    client: TestClient, wired: dict, settings: Settings, session: Session
) -> None:
    clip = Clip(
        episode_id=wired["episode"].id,
        idx=9,
        source_idx=9,
        duration_s=Decimal("6.00"),
        status="draft",
        shots_json=[{"no": 1, "start": "0", "end": "6", "desc": "主体推进"}],
        created_at=wired["clip"].created_at,
        updated_at=wired["clip"].updated_at,
    )
    session.add(clip)
    session.flush()
    login(client, wired["owner"], settings)

    response = client.post(f"/api/v1/clips/{clip.id}/steps/S4/run")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "clip.prompt" in body["written"]
    session.expire_all()
    refreshed = session.get(Clip, clip.id)
    assert refreshed.status == "prompted"
    assert refreshed.prompt and "镜头1" in refreshed.prompt


def test_context_carries_skill_sections(session: Session, settings: Settings, wired: dict) -> None:
    """上下文里必须真的有命中的 Skill 节，否则"知识层"就是摆设。"""
    project = session.get(Project, wired["project"].id)
    revision, _, section_count, trimmed, _ = steps.run_step(
        session, settings, stage="S1", target=project, provider=MockProvider(None)
    )
    assert section_count >= 1
    assert trimmed is False
    assert "适用规则" in (revision.prompt or "")
    assert "画幅" in (revision.prompt or "")


def test_lessons_are_recalled_into_context(session: Session, settings: Settings, wired: dict) -> None:
    """记忆写了要有人读：失败教训必须出现在下一次组装的上下文里（`03` §4.3）。"""
    from manju import lessons

    lessons.remember(
        session,
        scope=f"project:{wired['project'].id}",
        failure_type="motion_freeze",
        symptom="a2 末段冻结 1 处",
        fix="收尾保持位移，禁止静止定格",
    )
    project = session.get(Project, wired["project"].id)
    revision, _, _, _, _ = steps.run_step(
        session, settings, stage="S1", target=project, provider=MockProvider(None)
    )
    assert "踩过的坑" in (revision.prompt or "")
    assert "禁止静止定格" in (revision.prompt or "")


def test_bad_payload_is_rejected_by_schema(session: Session, settings: Settings, wired: dict) -> None:
    class Broken:
        name = "broken"

        def estimate(self, seconds):  # noqa: ANN001
            return Decimal(0)

        def video(self, **kwargs):  # noqa: ANN003
            raise AssertionError("不应调用 video")

        def text(self, *, stage, context, format_hint=""):  # noqa: ANN001
            from manju.providers import TextResult

            return TextResult(
                model="broken", payload={"logline": ""}, input_tokens=1, output_tokens=1,
                cost=Decimal(0), latency_ms=1,
            )

    project = session.get(Project, wired["project"].id)
    with pytest.raises(Exception) as excinfo:
        steps.run_step(session, settings, stage="S1", target=project, provider=Broken())
    assert "VALIDATION_INVARIANT" in str(excinfo.value) or "logline" in str(excinfo.value)


def test_skills_read_endpoints(client: TestClient, wired: dict, settings: Settings, session: Session) -> None:
    login(client, wired["owner"], settings)
    listed = client.get("/api/v1/skills").json()
    assert any(row["name"] == "story-to-script" for row in listed)
    skill = session.scalar(select(Skill).where(Skill.name == "story-to-script"))
    versions = client.get(f"/api/v1/skills/{skill.id}/versions").json()
    assert versions[0]["state"] == "active"
    assert len(versions[0]["sections"]) >= 2
    one = client.get(f"/api/v1/skills/{skill.id}/versions/{versions[0]['version']}")
    assert one.status_code == 200
    assert one.json()["checksum"] == versions[0]["checksum"]
