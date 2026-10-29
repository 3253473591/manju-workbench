"""P0-8 回滚：把片段指回某一次**已有**产物（`02` §6.2 / `03` §8.2）。

三条硬语义，各一个用例：
1. 回滚**不写新 revision**、不花钱，只改指针与 prompt/negative；
2. 审批绑定在具体 take 上 → 回到旧 take 必须重新 `review`（不能沿用上一次的通过）；
3. 幂等：已经是这一版就 200，`version` 不推进。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Asset, Clip, Episode, Project, Revision, User
from tests.conftest import login


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _take(session: Session, clip: Clip, *, prompt: str, cost: str = "1.000000") -> Revision:
    revision = Revision(
        target_kind="clip",
        target_id=clip.id,
        prompt=prompt,
        negative="无字幕",
        params_json={"res": "720p"},
        provider="mock",
        cost=Decimal(cost),
        created_at=_now(),
    )
    session.add(revision)
    session.flush()
    return revision


def _revision_count(session: Session, clip: Clip) -> int:
    return session.scalar(
        select(func.count()).select_from(Revision).where(
            Revision.target_id == clip.id, Revision.target_kind == "clip"
        )
    )


def _owner(session: Session, clip: Clip) -> User:
    """片段所属项目的 owner —— 用真数据反查，不在用例里写死 user id。"""
    episode = session.get(Episode, clip.episode_id)
    project = session.get(Project, episode.project_id)
    return session.get(User, project.owner_id)


@pytest.fixture
def two_takes(session: Session, world: dict) -> Clip:
    """approved 片段 + 两次**有产物**的 take，当前指向第二次。"""
    clip = world["clip"]
    first = _take(session, clip, prompt="镜头1（0-6秒）：远景急推")
    second = _take(session, clip, prompt="镜头1（0-6秒）：远景急推＋背景位移", cost="2.000000")
    for index, take in enumerate((first, second), start=1):
        asset = Asset(
            project_id=world["project"].id, kind="video", path=f"第一集/s{index}.mp4",
            sha256=str(index) * 64, bytes=1, source="gen", created_at=_now(),
        )
        session.add(asset)
        session.flush()
        take.output_asset_id = asset.id
    clip.current_revision_id = second.id
    clip.prompt = second.prompt
    clip.negative = second.negative
    clip.status = "approved"
    session.flush()
    clip.first_take_id = first.id  # type: ignore[attr-defined]
    return clip


def test_rollback_points_clip_back_without_new_revision(
    client: TestClient, session: Session, two_takes: Clip, settings: Settings
) -> None:
    clip = two_takes
    before = _revision_count(session, clip)
    version = clip.version
    login(client, _owner(session, clip), settings)

    response = client.post(f"/api/v1/clips/{clip.id}/rollback", json={"revision_id": clip.first_take_id})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_revision_id"] == clip.first_take_id
    assert body["prompt"] == "镜头1（0-6秒）：远景急推"
    assert body["negative"] == "无字幕"
    assert body["status"] == "review"  # 换回旧 take 必须回炉
    assert body["version"] == version + 1
    assert _revision_count(session, clip) == before  # 回滚不产生新 revision


def test_rollback_is_idempotent(
    client: TestClient, session: Session, two_takes: Clip, settings: Settings
) -> None:
    clip = two_takes
    version = clip.version
    login(client, _owner(session, clip), settings)
    response = client.post(
        f"/api/v1/clips/{clip.id}/rollback", json={"revision_id": clip.current_revision_id}
    )
    assert response.status_code == 200
    assert response.json()["version"] == version  # 已经是这一版，不动 version
    assert response.json()["status"] == "approved"


def test_rollback_to_foreign_or_missing_revision_is_404(
    client: TestClient, session: Session, world: dict, two_takes: Clip, settings: Settings
) -> None:
    clip = two_takes
    other = Clip(
        episode_id=world["episode"].id, idx=13, source_idx=13, duration_s=Decimal("6.00"),
        status="review", created_at=_now(), updated_at=_now(),
    )
    session.add(other)
    session.flush()
    foreign = _take(session, other, prompt="别的片段的 take")

    login(client, _owner(session, clip), settings)
    assert client.post(f"/api/v1/clips/{clip.id}/rollback", json={"revision_id": foreign.id}).status_code == 404
    assert client.post(f"/api/v1/clips/{clip.id}/rollback", json={"revision_id": 999999}).status_code == 404


def test_rollback_requires_write_permission(
    client: TestClient, session: Session, world: dict, two_takes: Clip, settings: Settings
) -> None:
    login(client, world["viewer"], settings)
    response = client.post(
        f"/api/v1/clips/{two_takes.id}/rollback", json={"revision_id": two_takes.first_take_id}
    )
    assert response.status_code == 403


def test_text_only_revision_restores_prompt_without_status_change(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """S4 的 revision 没有产物：回滚它只恢复提示词，不该把 `prompted` 的片段推进 review。"""
    clip = Clip(
        episode_id=world["episode"].id, idx=15, source_idx=15, duration_s=Decimal("6.00"),
        status="prompted", prompt="新提示词",
        shots_json=[{"no": 1, "start": 0, "end": 6, "desc": "远景急推"}],
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    old = Revision(
        target_kind="clip", target_id=clip.id, prompt="旧提示词", negative="无字幕",
        params_json={"stage": "S4"}, provider="mock", cost=Decimal(0), created_at=_now(),
    )
    session.add(old)
    session.flush()

    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/clips/{clip.id}/rollback", json={"revision_id": old.id})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "prompted"
    assert response.json()["prompt"] == "旧提示词"


def test_rollback_to_take_from_draft_is_409(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """draft 片段回滚到一次**有产物**的 take：draft → review 非法，必须 409。"""
    draft = Clip(
        episode_id=world["episode"].id, idx=14, source_idx=14, duration_s=Decimal("6.00"),
        status="draft", created_at=_now(), updated_at=_now(),
    )
    session.add(draft)
    session.flush()
    take = _take(session, draft, prompt="幽灵 take")
    asset = Asset(
        project_id=world["project"].id, kind="video", path="第一集/s1.mp4", sha256="b" * 64,
        bytes=1, source="gen", created_at=_now(),
    )
    session.add(asset)
    session.flush()
    take.output_asset_id = asset.id
    session.flush()

    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/clips/{draft.id}/rollback", json={"revision_id": take.id})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "JOB_STATE_INVALID"


def test_rollback_moves_output_asset_to_target_take(
    client: TestClient, session: Session, world: dict, two_takes: Clip, settings: Settings
) -> None:
    """回滚后镜头墙的海报要跟着变 —— `output_asset_id` 取自 `current_revision_id`。"""
    clip = two_takes
    asset = Asset(
        project_id=world["project"].id, kind="video", path="第一集/s1.mp4", sha256="a" * 64,
        bytes=1, source="gen", created_at=_now(),
    )
    session.add(asset)
    session.flush()
    session.get(Revision, clip.first_take_id).output_asset_id = asset.id
    session.flush()

    login(client, _owner(session, clip), settings)
    response = client.post(f"/api/v1/clips/{clip.id}/rollback", json={"revision_id": clip.first_take_id})
    assert response.status_code == 200
    assert response.json()["output_asset_id"] == asset.id
