"""跨集资产复用：**图片资产归项目，不归集**（`01` §4.5）。

这条语义现在只写在文档与 `_asset_problems` 的实现里，没有用例兜着 —— 一旦有人"顺手"给
asset 加个 `episode_id` 或把槽位校验收紧成"必须同集"，跨集复用就会静默失效。
所以这里把两个方向都钉死：同项目**跨集**放行、跨项目拒绝。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Asset, Clip, Episode
from tests.conftest import login, make_user


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _clip(session: Session, episode_id: int, *, idx: int) -> Clip:
    clip = Clip(
        episode_id=episode_id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"), status="draft",
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return clip


def test_image_asset_from_another_episode_can_be_referenced(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """第二集的分镜引用第一集成图（同一张角色参考图）：合法，资产不复制。"""
    second = Episode(
        project_id=world["project"].id, idx=2, title="第二集", status="draft", version=1,
        created_at=_now(), updated_at=_now(),
    )
    session.add(second)
    session.flush()
    # 资产只挂项目，没有 episode_id —— 它是全剧共享的
    asset = Asset(
        project_id=world["project"].id, kind="image", path="第一集/角色.png", sha256="e" * 64,
        bytes=1024, source="upload", created_at=_now(),
    )
    session.add(asset)
    session.flush()
    clip = _clip(session, second.id, idx=1)

    login(client, world["owner"], settings)
    response = client.patch(
        f"/api/v1/clips/{clip.id}",
        json={"ref_slots": [{"idx": 1, "kind": "image", "role": "造型锚", "asset_id": asset.id}]},
        headers={"If-Match": '"1"'},
    )
    assert response.status_code == 200, response.text
    slot = response.json()["ref_slots"][0]
    assert slot["asset_id"] == asset.id
    assert slot["url"] and slot["thumb_url"]  # 出参直接给取图地址，前端不用再查资产


def test_asset_of_another_project_is_rejected(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    other = make_user(session, "other@example.com", "lead")
    from manju.models import Project

    foreign = Project(
        title="别人的项目", owner_id=other.id, status="active", version=1,
        created_at=_now(), updated_at=_now(),
    )
    session.add(foreign)
    session.flush()
    asset = Asset(
        project_id=foreign.id, kind="image", path="x.png", sha256="f" * 64, bytes=1,
        source="upload", created_at=_now(),
    )
    session.add(asset)
    session.flush()
    clip = _clip(session, world["episode"].id, idx=11)

    login(client, world["owner"], settings)
    response = client.patch(
        f"/api/v1/clips/{clip.id}",
        json={"ref_slots": [{"idx": 1, "kind": "image", "role": "造型锚", "asset_id": asset.id}]},
        headers={"If-Match": '"1"'},
    )
    assert response.status_code == 422, response.text
    assert "别的项目" in " ".join(response.json()["error"]["detail"]["problems"])


def test_asset_list_is_project_scoped_across_episodes(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """资产库按项目查：一次能捞出所有集的图，这正是"跨集挑同一张参考图"的入口。"""
    for index, sha in enumerate(("1", "2"), start=1):
        session.add(
            Asset(
                project_id=world["project"].id, kind="image", path=f"第一集/a{index}.png",
                sha256=sha * 64, bytes=10, source="upload", created_at=_now(),
            )
        )
    session.flush()

    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/assets?project_id={world['project'].id}&kind=image")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert all(item["kind"] == "image" for item in response.json()["items"])
