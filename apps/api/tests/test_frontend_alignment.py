"""前端对接清单（`docs/前端视觉交付与对接清单.md`）里点到后端的每一项，逐条钉住。

§2.1 #1–#3（槽位取图地址、缩略图宽高、视频 Range）与 §2.2 #4–#5（每片段成本、版本序号）
都在这里；纯前端的活（三态、行内编辑、diff 抽屉）不在本文件范围。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Asset, Clip, Revision
from tests.conftest import login

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _poster(session: Session, world: dict, media_root: Path) -> Asset:
    (media_root / "demo").mkdir(exist_ok=True)
    (media_root / "demo" / "poster.png").write_bytes(PNG)
    asset = Asset(
        project_id=world["project"].id,
        kind="image",
        path="demo/poster.png",
        sha256="b" * 64,
        bytes=len(PNG),
        w=1280,
        h=720,
        thumb_w=720,
        thumb_h=405,
        source="seed",
        created_at=_now(),
    )
    session.add(asset)
    session.flush()
    return asset


def test_slot_urls_are_derived_not_stored(
    client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path
) -> None:
    """§2.1 #1：槽位带出取图地址；客户端传进来的 `url` 不落库。"""
    poster = _poster(session, world, media_root)
    login(client, world["owner"], settings)
    patched = client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        json={
            "ref_slots": [
                {
                    "idx": 1,
                    "kind": "image",
                    "asset_id": poster.id,
                    "role": "实景",
                    "url": "http://evil.example/forged.png",
                }
            ]
        },
        headers={"If-Match": '"1"'},
    )
    assert patched.status_code == 200
    slot = patched.json()["ref_slots"][0]
    assert slot["url"] == f"/api/v1/assets/{poster.id}/file"
    assert slot["thumb_url"] == f"/api/v1/assets/{poster.id}/file?thumb=1"

    stored = session.get(Clip, world["clip"].id).ref_slots_json[0]
    assert "url" not in stored and "thumb_url" not in stored


def test_unresolved_slot_has_no_url(client: TestClient, world: dict, settings: Settings, session: Session) -> None:
    """没回填 `asset_id` 的槽位不许给出假地址 —— 前端据此显示文件名占位。"""
    clip = session.get(Clip, world["clip"].id)
    clip.ref_slots_json = [{"idx": 1, "kind": "image", "asset_id": None, "asset_ref": "sk1_scene.png", "role": "实景", "note": ""}]
    session.flush()
    login(client, world["owner"], settings)
    slot = client.get(f"/api/v1/clips/{clip.id}").json()["ref_slots"][0]
    assert slot["url"] is None
    assert slot["asset_ref"] == "sk1_scene.png"


def test_thumb_dimensions_exposed(client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path) -> None:
    """§2.1 #2：前端要 `thumb_w/thumb_h` 留占位比例。"""
    poster = _poster(session, world, media_root)
    login(client, world["owner"], settings)
    body = client.get(f"/api/v1/assets/{poster.id}").json()
    assert (body["w"], body["h"]) == (1280, 720)
    assert (body["thumb_w"], body["thumb_h"]) == (720, 405)


def test_asset_file_supports_range(client: TestClient, world: dict, settings: Settings, session: Session, media_root: Path) -> None:
    """§2.1 #3：视频预览要能拖动，`FileResponse` 必须回 206。"""
    poster = _poster(session, world, media_root)
    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/assets/{poster.id}/file", headers={"Range": "bytes=0-9"})
    assert response.status_code == 206
    assert response.headers["content-range"].startswith("bytes 0-9/")
    assert len(response.content) == 10


def test_clip_cost_total(client: TestClient, world: dict, settings: Settings, session: Session) -> None:
    """§2.2 #4：表格/镜头墙要每片段金额，不必自己去聚合。"""
    for cost in ("2.27", "1.27"):
        session.add(
            Revision(
                target_kind="clip",
                target_id=world["clip"].id,
                cost=Decimal(cost),
                created_at=_now(),
            )
        )
    session.flush()
    login(client, world["owner"], settings)
    assert client.get(f"/api/v1/clips/{world['clip'].id}").json()["cost_total"] == "3.540000"
    listed = client.get(f"/api/v1/episodes/{world['episode'].id}/clips").json()
    assert {item["id"]: item["cost_total"] for item in listed}[world["clip"].id] == "3.540000"


def test_cost_group_by_clip(client: TestClient, world: dict, settings: Settings, session: Session) -> None:
    session.add(
        Revision(
            target_kind="clip",
            target_id=world["clip"].id,
            model_used="seedance-2.0-mini",
            cost=Decimal("2.270000"),
            video_seconds=Decimal("12.00"),
            created_at=_now(),
        )
    )
    session.flush()
    login(client, world["owner"], settings)
    body = client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by=clip").json()
    assert body["group_by"] == "clip"
    assert body["items"][0]["key"] == f"clip:{world['clip'].id}"
    assert body["items"][0]["cost"] == "2.270000"


def test_clip_without_revisions_has_zero_cost(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    created = client.post(f"/api/v1/episodes/{world['episode'].id}/clips", json={"duration_s": "5.00"})
    assert created.status_code == 201
    assert created.json()["cost_total"] == "0.000000"


def test_revision_seq_is_per_target(client: TestClient, world: dict, settings: Settings, session: Session) -> None:
    """§2.2 #5：`revision` 没有 version 列，序号按 id 顺序算成派生字段。"""
    ids = []
    for cost in ("1.00", "2.00", "3.00"):
        revision = Revision(
            target_kind="clip",
            target_id=world["clip"].id,
            cost=Decimal(cost),
            created_at=_now(),
        )
        session.add(revision)
        session.flush()
        ids.append(revision.id)
    # 另一个片段的 revision 不应打乱本片段的序号
    session.add(Revision(target_kind="clip", target_id=world["clip"].id + 1000, cost=Decimal("9.00"), created_at=_now()))
    session.flush()

    login(client, world["owner"], settings)
    items = client.get(f"/api/v1/clips/{world['clip'].id}/revisions").json()["items"]
    assert {item["id"]: item["seq"] for item in items} == {ids[0]: 1, ids[1]: 2, ids[2]: 3}
    assert client.get(f"/api/v1/revisions/{ids[2]}").json()["seq"] == 3
