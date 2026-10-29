"""对抗性探针 2：状态机、相邻资源、出参侧脏数据。

第一批打的是单个入口的形状；这一批打**状态与跨资源**：
- 非法状态跳转必须 409，不能靠 DB 的 CHECK 炸成 500；
- 生命周期端点（confirm/cancel/retry）在两个方向上都撞一遍，含重复调用；
- 越权读别的项目的资产、给不存在的用户加成员、第 51 集 / 第 31 个片段这类边界；
- 出参侧脏数据（`acceptance_result` 出现非法 verdict）必须被 schema 挡住，而不是原样吐出。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Asset, Clip, Episode, Project, ProjectMember
from tests.conftest import login, make_user


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _clip(session: Session, episode_id: int, *, idx: int = 9, status: str = "draft") -> Clip:
    """造一个**自洽**的片段：有闭合分镜，否则 PATCH 会先被域不变量挡掉（422），
    探针就撞不到状态机那一层。"""
    clip = Clip(
        episode_id=episode_id,
        idx=idx,
        source_idx=idx,
        duration_s=Decimal("6.00"),
        status=status,
        shots_json=[{"no": 1, "start": 0, "end": 6, "desc": "远景急推"}],
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return clip


def _gen(client: TestClient, clip: Clip, key: str, *, version: int | None = None, mode: str = "default"):
    return client.post(
        f"/api/v1/clips/{clip.id}/generate?mode={mode}",
        json={"res": "draft", "budget_limit": "12.000000"},
        headers={"Idempotency-Key": key, "If-Match": f'"{clip.version if version is None else version}"'},
    )


# ---------------------------------------------------------------- 状态机

@pytest.mark.parametrize(
    "current,target",
    [
        ("approved", "generating"),
        ("approved", "prompted"),
        ("approved", "draft"),
        ("draft", "approved"),
        ("review", "draft"),
        ("generating", "approved"),
        ("rejected", "prompted"),
    ],
)
def test_illegal_clip_transitions_are_409_not_500(
    client: TestClient, session: Session, world: dict, settings: Settings, current: str, target: str
) -> None:
    clip = _clip(session, world["episode"].id, status=current)
    login(client, world["owner"], settings)
    response = client.patch(f"/api/v1/clips/{clip.id}", json={"status": target}, headers={"If-Match": '"1"'})
    assert response.status_code == 409, f"{current}→{target} 得到 {response.status_code} {response.text[:200]}"
    assert response.json()["error"]["code"] == "JOB_STATE_INVALID"


def test_same_status_is_idempotent(client: TestClient, session: Session, world: dict, settings: Settings) -> None:
    clip = _clip(session, world["episode"].id, status="review")
    login(client, world["owner"], settings)
    response = client.patch(f"/api/v1/clips/{clip.id}", json={"status": "review"}, headers={"If-Match": '"1"'})
    assert response.status_code == 200


# ---------------------------------------------------------------- job 生命周期

def test_confirm_on_queued_job_is_409(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    job = _gen(client, world["clip"], "auto-1", mode="auto")
    assert job.status_code == 202, job.text
    confirmed = client.post(f"/api/v1/jobs/{job.json()['job']['id']}/confirm")
    assert confirmed.status_code == 409, confirmed.text


def test_cancel_twice_and_retry_after_cancel(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    job_id = _gen(client, world["clip"], "cancel-1").json()["job"]["id"]
    first = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert first.status_code == 200 and first.json()["state"] == "cancelled"
    again = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert again.status_code == 409 and again.json()["error"]["code"] == "JOB_NOT_CANCELLABLE"
    retried = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retried.status_code == 409, retried.text


def test_retry_on_waiting_confirm_is_409(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    job_id = _gen(client, world["clip"], "retry-waiting").json()["job"]["id"]
    response = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert response.status_code == 409, response.text


def test_generate_on_archived_project_is_blocked(
    client: TestClient, world: dict, settings: Settings
) -> None:
    login(client, world["owner"], settings)
    archived = client.post(f"/api/v1/projects/{world['project'].id}/archive")
    assert archived.status_code == 200
    response = _gen(client, world["clip"], "archived-1")
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PERM_READONLY_PROJECT"


def test_generate_with_absurd_budgets(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    for budget, expected in [
        ("0", 422), ("-1", 422), ("0.001", 422), ("0.01", 202),
        ("100.000000", 202), ("100.000001", 422), ("1e10", 422), ("NaN", 422),
    ]:
        response = client.post(
            f"/api/v1/clips/{world['clip'].id}/generate",
            json={"res": "draft", "budget_limit": budget},
            headers={"Idempotency-Key": f"budget-{budget}", "If-Match": '"1"'},
        )
        assert response.status_code == expected, f"{budget} → {response.status_code} {response.text[:160]}"


# ---------------------------------------------------------------- steps

def test_steps_on_missing_targets_are_404(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    assert client.post("/api/v1/projects/999999/steps/S1/run").status_code == 404
    assert client.post("/api/v1/episodes/999999/steps/S3/run").status_code == 404
    assert client.post("/api/v1/clips/999999/steps/S4/run").status_code == 404


def test_steps_unknown_stage_is_not_500(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/projects/{world['project'].id}/steps/S9/run")
    assert response.status_code in (404, 405), response.status_code


def test_s3_rerun_is_conflict_not_duplicate(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/episodes/{world['episode'].id}/steps/S3/run")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT_CLIP_INDEX"


def test_s4_on_clip_without_shots_is_422(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """分镜没填就调 S4：必须 422，不能让空分镜的片段拿到提示词（那会让 domain 的
    `require_shots` 变成摆设）。"""
    clip = Clip(
        episode_id=world["episode"].id, idx=12, source_idx=12, duration_s=Decimal("6.00"),
        status="draft", created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/clips/{clip.id}/steps/S4/run")
    assert response.status_code == 422, response.text
    assert "shots 不能为空" in response.json()["error"]["detail"]["problems"][0]


def test_steps_by_viewer_are_forbidden(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["viewer"], settings)
    response = client.post(f"/api/v1/projects/{world['project'].id}/steps/S1/run")
    assert response.status_code == 403


# ---------------------------------------------------------------- Skill 写路径的坏请求

def test_skill_write_attacks(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["admin"], settings)
    base = f"/api/v1/skills/{world['project'].id}"  # 不存在的 skill
    assert client.post(f"{base}/versions", json={"body_md": "## a\nb"}).status_code == 404
    assert client.post(f"{base}/versions/1/dry-run", json={"clip_id": 1}).status_code == 404
    assert client.post(f"{base}/rollback", json={"version": 1}).status_code == 404
    assert client.patch(base, json={"status": "garbage"}).status_code in (404, 422)


def test_rollback_to_active_version_is_idempotent(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    from manju import skills as skill_lib
    from manju.models import Skill

    skill = Skill(name="s4-skill", stage="S4", status="active", created_at=_now())
    session.add(skill)
    session.flush()
    version, _ = skill_lib.upsert_skill(
        session, name=skill.name, stage=skill.stage, body_md="## a\n正文", created_by=world["admin"].id
    )
    login(client, world["admin"], settings)
    response = client.post(f"/api/v1/skills/{skill.id}/rollback", json={"version": version.version})
    assert response.status_code == 200, response.text
    assert response.json()["superseded"] is None


# ---------------------------------------------------------------- 资产：越权与坏参数

def test_asset_param_and_permission_attacks(
    client: TestClient, session: Session, world: dict, settings: Settings, media_root: Path
) -> None:
    (media_root / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    asset = Asset(
        project_id=world["project"].id, kind="image", path="a.jpg", sha256="b" * 64, bytes=4,
        source="upload", created_at=_now(),
    )
    session.add(asset)
    session.flush()

    login(client, world["owner"], settings)
    assert client.get(f"/api/v1/assets/{asset.id}/file?thumb=abc").status_code == 422
    assert client.get(f"/api/v1/assets/{asset.id}/file?thumb=1").status_code == 200
    assert client.get("/api/v1/assets").status_code == 422  # 缺 project_id
    assert client.get("/api/v1/assets?project_id=0").status_code == 422
    assert client.get("/api/v1/assets?project_id=999999").status_code == 404

    # 读任意项目是**文档明确的产品决定**（`02` §2.2：三档角色都能读任意项目），
    # 所以外人拿到 200 是设计而不是漏洞；写才是按项目收口
    outsider = make_user(session, "outsider@example.com", "user")
    login(client, outsider, settings)
    assert client.get(f"/api/v1/assets/{asset.id}/file").status_code == 200
    assert client.patch(
        f"/api/v1/clips/{world['clip'].id}", json={"title": "x"}, headers={"If-Match": '"3"'}
    ).status_code == 403
    # admin 内容只读但**读**是全项目可见
    login(client, world["admin"], settings)
    assert client.get(f"/api/v1/assets/{asset.id}/file").status_code == 200


def test_asset_path_escape_does_not_leak_files(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """路径收敛是唯一的遍历面：DB 里塞 `../` 也绝不能把仓库外文件吐出去。"""
    asset = Asset(
        project_id=world["project"].id, kind="image", path="../../../../Windows/win.ini",
        sha256="c" * 64, bytes=1, source="upload", created_at=_now(),
    )
    session.add(asset)
    session.flush()
    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/assets/{asset.id}/file")
    assert response.status_code != 200
    assert b"[fonts]" not in response.content


# ---------------------------------------------------------------- 项目 / 集 / 成员 / 片段边界

def test_project_title_boundaries(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    assert client.post("/api/v1/projects", json={"title": ""}).status_code == 422
    assert client.post("/api/v1/projects", json={"title": "   "}).status_code == 422
    assert client.post("/api/v1/projects", json={"title": "\u200b\u200b"}).status_code == 422
    assert client.post("/api/v1/projects", json={"title": "题" * 200}).status_code == 201
    assert client.post("/api/v1/projects", json={"title": "题" * 201}).status_code == 422


def test_membership_attacks(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = f"/api/v1/projects/{world['project'].id}/members"
    assert client.post(url, json={"email": "owner@example.com", "role": "owner"}).status_code == 422
    assert client.post(url, json={"email": "ghost@example.com", "role": "member"}).status_code == 404
    # owner 不落 project_member（所有权只有一处真相）→ 422，不是"加了个成员"
    assert client.post(url, json={"email": "owner@example.com", "role": "member"}).status_code == 422
    assert client.delete(f"{url}/999999").status_code == 404
    # 非成员（含 viewer）改成员表 → 403
    login(client, world["viewer"], settings)
    assert client.post(url, json={"email": "member@example.com", "role": "member"}).status_code == 403


def test_episode_and_clip_index_overflow(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    project_id = world["project"].id
    episode_id = world["episode"].id
    for idx in range(2, 51):
        session.add(
            Episode(
                project_id=project_id, idx=idx, title=f"第{idx}集", status="draft", version=1,
                created_at=_now(), updated_at=_now(),
            )
        )
    for idx in range(2, 31):
        session.add(
            Clip(
                episode_id=episode_id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"),
                status="draft", created_at=_now(), updated_at=_now(),
            )
        )
    session.flush()

    login(client, world["owner"], settings)
    too_many = client.post(f"/api/v1/projects/{project_id}/episodes", json={"title": "第51集"})
    assert too_many.status_code == 422, too_many.text
    clip_over = client.post(
        f"/api/v1/episodes/{episode_id}/clips", json={"duration_s": "6.00"}
    )
    assert clip_over.status_code == 422, clip_over.text


def test_clip_duration_boundaries(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = f"/api/v1/episodes/{world['episode'].id}/clips"
    assert client.post(url, json={"duration_s": "0.99"}).status_code == 422
    assert client.post(url, json={"duration_s": "15.01"}).status_code == 422
    assert client.post(url, json={"duration_s": "1.00"}).status_code == 201
    assert client.post(url, json={"duration_s": "15.00"}).status_code == 201


def test_pagination_attacks(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = "/api/v1/projects"  # 这套接口里带游标分页的是 /projects 与 /jobs
    clamped = client.get(f"{url}?limit=999999")
    assert clamped.status_code == 200 and isinstance(clamped.json()["items"], list)
    assert len(clamped.json()["items"]) <= 200
    empty = client.get(f"{url}?cursor=-1")
    assert empty.status_code == 200 and empty.json()["items"] == []
    # 游标语义是 `id < cursor`：指向比所有 id 都小的地方就是空页（已删记录的情形同此）
    below_all = client.get(f"{url}?cursor=1")
    assert below_all.status_code == 200 and below_all.json()["items"] == []
    # 而给一个比所有 id 都大的游标 = 从最新开始，这是游标语义的正常结果
    newest = client.get(f"{url}?cursor=999999999")
    assert newest.status_code == 200 and newest.json()["items"]


# ---------------------------------------------------------------- 出参侧脏数据

def test_revision_out_rejects_illegal_verdict() -> None:
    from manju.schemas import RevisionOut

    with pytest.raises(ValidationError):
        RevisionOut.model_validate(
            {
                "id": 1, "target_kind": "clip", "target_id": 1, "cost": "0.000000",
                "acceptance_result": [{"id": "a1", "result": "maybe", "evidence": "x"}],
            }
        )


def test_clip_out_rejects_dirty_json() -> None:
    """出参侧只挡**形状**，不挡取值范围 —— 一条历史脏行不该把整个列表端点打成 500。

    `duration_s="0"` 会被放过（渲染成 0s，难看但不崩）；要拦住它的是 domain 的写入校验。
    """
    from manju.schemas import ClipOut

    base = {
        "id": 1, "episode_id": 1, "idx": 1, "source_idx": 1, "title": None,
        "duration_s": "6.00", "status": "draft", "prompt": None, "negative": None,
        "shots": None, "ref_slots": None, "dialogue": None, "acceptance": None,
        "current_revision_id": None, "parent_clip_id": None, "version": 1,
        "created_at": "2026-01-01T00:00:00.000Z", "updated_at": "2026-01-01T00:00:00.000Z",
    }
    with pytest.raises(ValidationError):
        ClipOut.model_validate({**base, "shots": [{"no": 1}]})
    assert ClipOut.model_validate({**base, "duration_s": "0"}).duration_s == Decimal("0")
