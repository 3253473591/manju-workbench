"""Skill 写路径：新建版本 → 试跑 → 发布 → 回滚（`02` §5.5 / §6.3）。

这条路径的核心是**单向门**：没试跑过的版本发布不了，所以"改完提示词直接上线"不可能发生。
另外两条边界值得单独钉住：正文带脚本围栏要 422，试跑目标类型不对也要 422（不是"自己挑一个"）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import skills as skill_lib
from manju.config import Settings
from manju.models import Skill, SkillVersion
from tests.conftest import login

BODY = """## #110 巨物压迫感四件套
远景先给高度差，人只占画面十分之一。

## #112 机位位移＋背景相对位移
主体在动，背景也要有相对位移。
"""

BODY_V2 = BODY + """
## #117 文案侧内容安全
不出现真实品牌与平台名。
"""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _skill(session: Session, *, name: str = "seedance-final-prompt", stage: str = "S4") -> Skill:
    skill = Skill(name=name, stage=stage, status="active", created_at=_now())
    session.add(skill)
    session.flush()
    return skill


def _active(session: Session, skill: Skill) -> SkillVersion | None:
    return session.scalar(
        select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.state == "active")
    )


def test_create_version_parses_sections_and_dedupes_by_checksum(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    login(client, world["admin"], settings)
    skill = _skill(session)
    url = f"/api/v1/skills/{skill.id}/versions"

    created = client.post(url, json={"body_md": BODY, "note": "四件套"})
    assert created.status_code == 201
    payload = created.json()["skill_version"]
    assert payload["state"] == "draft"
    assert payload["sections"] == 2
    assert payload["tokens_total"] > 0
    assert len(payload["checksum"]) == 64

    again = client.post(url, json={"body_md": BODY})
    assert again.status_code == 200  # 同 checksum 不刷版本号
    assert again.json()["skill_version"]["id"] == payload["id"]


def test_write_endpoints_are_admin_only(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    login(client, world["owner"], settings)  # lead 也写不了 Skill
    skill = _skill(session)
    denied = client.post(f"/api/v1/skills/{skill.id}/versions", json={"body_md": BODY})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERM_ADMIN_ONLY"


def test_body_with_script_fence_is_rejected(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    login(client, world["admin"], settings)
    skill = _skill(session)
    body = "## #110 巨物\n先用脚本算一遍\n\n```sh\nffmpeg -i in.mp4 out.mp4\n```\n"
    response = client.post(f"/api/v1/skills/{skill.id}/versions", json={"body_md": body})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SKILL_TOOL_UNKNOWN"


def test_publish_requires_dry_run_and_supersedes_previous(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    login(client, world["admin"], settings)
    skill = _skill(session)
    clip_id = world["clip"].id
    base = f"/api/v1/skills/{skill.id}/versions"

    first = client.post(base, json={"body_md": BODY}).json()["skill_version"]
    blocked = client.post(f"{base}/{first['version']}/publish")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "SKILL_NOT_DRYRUN"

    wrong_target = client.post(f"{base}/{first['version']}/dry-run", json={"episode_id": world["episode"].id})
    assert wrong_target.status_code == 422
    assert wrong_target.json()["error"]["code"] == "VALIDATION_SHAPE"

    preview = client.post(f"{base}/{first['version']}/dry-run", json={"clip_id": clip_id})
    assert preview.status_code == 200
    body = preview.json()
    assert body["injected"] and all({"rule_id", "title", "tokens_est", "selected"} <= set(item) for item in body["injected"])
    assert body["budget"]["input_limit"] == 12_000
    assert body["budget"]["used_total"] > 0
    assert "巨物压迫感四件套" in body["prompt_preview"]
    # 库里还没有 active 版本，草稿的全部规则都算新增
    assert set(body["diff_vs_active"]["added_rules"]) == {"#110", "#112"}
    assert body["diff_vs_active"]["removed_rules"] == []

    published = client.post(f"{base}/{first['version']}/publish")
    assert published.status_code == 200
    assert published.json() == {"skill": {"id": skill.id, "active_version": first["version"]}, "superseded": None}
    assert _active(session, skill).version == first["version"]

    second = client.post(base, json={"body_md": BODY_V2, "note": "补内容安全"}).json()["skill_version"]
    diff = client.post(f"{base}/{second['version']}/dry-run", json={"clip_id": clip_id}).json()
    assert diff["diff_vs_active"]["added_rules"] == ["#117"]
    assert diff["diff_vs_active"]["removed_rules"] == []
    assert diff["diff_vs_active"]["tokens_delta"] > 0

    replaced = client.post(f"{base}/{second['version']}/publish")
    assert replaced.json()["superseded"] == first["version"]
    assert _active(session, skill).version == second["version"]
    old = session.scalar(
        select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.version == first["version"])
    )
    assert old.state == "superseded" and old.superseded_at is not None


def test_rollback_restores_previous_version(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    skill = _skill(session)
    clip_id = world["clip"].id
    first, _ = skill_lib.upsert_skill(
        session, name=skill.name, stage=skill.stage, body_md=BODY, created_by=world["admin"].id
    )
    second = _skill_version(session, skill, BODY_V2, world["admin"].id)
    login(client, world["admin"], settings)
    base = f"/api/v1/skills/{skill.id}/versions"

    client.post(f"{base}/{second.version}/dry-run", json={"clip_id": clip_id})
    client.post(f"{base}/{second.version}/publish")
    assert _active(session, skill).version == second.version

    rolled = client.post(f"/api/v1/skills/{skill.id}/rollback", json={"version": first.version})
    assert rolled.status_code == 200
    assert rolled.json() == {"skill": {"id": skill.id, "active_version": first.version}, "superseded": second.version}
    assert _active(session, skill).version == first.version


def test_dry_run_rejects_stage_without_step_template(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    login(client, world["admin"], settings)
    review_skill = _skill(session, name="review-rules", stage="review")
    version = _skill_version(session, review_skill, BODY, world["admin"].id)
    response = client.post(
        f"/api/v1/skills/{review_skill.id}/versions/{version.version}/dry-run", json={"clip_id": world["clip"].id}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_RANGE"


def test_patch_disables_skill(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    login(client, world["admin"], settings)
    skill = _skill(session)
    disabled = client.patch(f"/api/v1/skills/{skill.id}", json={"status": "disabled"})
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"


def test_dry_run_picks_stage_by_binding(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """`story-to-script` 一行服务 S1/S2/S3（name 唯一，拆不成三行）：
    同一个版本要能按项目、也能按集试注入，且两次组装出来的上下文真的不同。"""
    login(client, world["admin"], settings)
    skill = _skill(session, name="story-to-script", stage="S3")
    version = _skill_version(session, skill, BODY, world["admin"].id)
    base = f"/api/v1/skills/{skill.id}/versions/{version.version}/dry-run"

    by_project = client.post(base, json={"project_id": world["project"].id})
    by_episode = client.post(base, json={"episode_id": world["episode"].id})
    assert by_project.status_code == 200, by_project.text
    assert by_episode.status_code == 200, by_episode.text
    assert by_project.json()["prompt_preview"] != by_episode.json()["prompt_preview"]

    off_binding = client.post(base, json={"clip_id": world["clip"].id})
    assert off_binding.status_code == 422, off_binding.text
    assert off_binding.json()["error"]["code"] == "VALIDATION_SHAPE"


def test_dry_run_on_unbound_skill_is_422(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """没有被任何 `StageSpec.skill` 引用的技能（种子里的 krea2-character-card）试跑无从谈起。"""
    login(client, world["admin"], settings)
    skill = _skill(session, name="krea2-character-card", stage="S5")
    version = _skill_version(session, skill, BODY, world["admin"].id)
    response = client.post(
        f"/api/v1/skills/{skill.id}/versions/{version.version}/dry-run",
        json={"project_id": world["project"].id},
    )
    assert response.status_code == 422
    assert "没有被任何 step 绑定" in response.json()["error"]["detail"]["reason"]


def _skill_version(session: Session, skill: Skill, body_md: str, created_by: int) -> SkillVersion:
    version, _ = skill_lib.create_version(session, skill=skill, body_md=body_md, created_by=created_by)
    return version
