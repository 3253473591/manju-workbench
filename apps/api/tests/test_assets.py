"""资产接口与 `clip.title`（`02` §3.9 / §3.3）。

原型上这两条是"标注待补"的直接来源，所以钉得细一点：元信息形状、取图字节、缩略图回落、
路径越界、以及标题能不能落库并回读。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Asset, Clip
from tests.conftest import login

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def poster(session: Session, world: dict, media_root: Path) -> Asset:
    folder = media_root / "demo"
    folder.mkdir(exist_ok=True)
    (folder / "poster.png").write_bytes(PNG)
    asset = Asset(
        project_id=world["project"].id,
        kind="image",
        path="demo/poster.png",
        sha256="b" * 64,
        bytes=len(PNG),
        w=1280,
        h=720,
        source="seed",
        created_at=_now(),
    )
    session.add(asset)
    session.flush()
    return asset


def test_asset_metadata_shape(client: TestClient, world: dict, settings: Settings, poster: Asset) -> None:
    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/assets/{poster.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "image"
    assert body["path"] == "demo/poster.png"
    assert body["thumb_path"] is None
    assert body["url"] == f"/api/v1/assets/{poster.id}/file"
    assert body["thumb_url"] == f"/api/v1/assets/{poster.id}/file?thumb=1"
    assert body["created_at"].endswith("Z")


def test_asset_file_serves_bytes(client: TestClient, world: dict, settings: Settings, poster: Asset) -> None:
    login(client, world["owner"], settings)
    response = client.get(poster_url(poster))
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"].startswith("private")


def test_thumb_falls_back_to_original(client: TestClient, world: dict, settings: Settings, poster: Asset) -> None:
    login(client, world["owner"], settings)
    response = client.get(poster_url(poster), params={"thumb": 1})
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["X-Thumb-Fallback"] == "1"


def test_thumb_used_when_present(
    client: TestClient, world: dict, settings: Settings, session: Session, poster: Asset, media_root: Path
) -> None:
    thumb = PNG + b"thumb"
    (media_root / "demo" / "poster-thumb.png").write_bytes(thumb)
    poster.thumb_path = "demo/poster-thumb.png"
    session.flush()
    login(client, world["owner"], settings)
    response = client.get(poster_url(poster), params={"thumb": 1})
    assert response.status_code == 200
    assert response.content == thumb
    assert "X-Thumb-Fallback" not in response.headers
    assert client.get(f"/api/v1/assets/{poster.id}").json()["thumb_path"] == "demo/poster-thumb.png"


def test_asset_path_cannot_escape_media_root(
    client: TestClient, world: dict, settings: Settings, session: Session
) -> None:
    escape = Asset(
        project_id=world["project"].id,
        kind="image",
        path="../outside.png",
        sha256="c" * 64,
        bytes=1,
        source="seed",
        created_at=_now(),
    )
    session.add(escape)
    session.flush()
    login(client, world["owner"], settings)
    denied = client.get(f"/api/v1/assets/{escape.id}/file")
    assert denied.status_code == 500
    assert denied.json()["error"]["code"] == "INTERNAL"
    assert denied.json()["error"]["detail"]["reason"] == "asset 越出 MEDIA_ROOT"


def test_missing_file_is_404(client: TestClient, world: dict, settings: Settings, session: Session) -> None:
    ghost = Asset(
        project_id=world["project"].id,
        kind="video",
        path="demo/nope.mp4",
        sha256="d" * 64,
        bytes=1,
        source="seed",
        created_at=_now(),
    )
    session.add(ghost)
    session.flush()
    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/assets/{ghost.id}/file")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_asset_requires_login(client: TestClient, poster: Asset) -> None:
    assert client.get(f"/api/v1/assets/{poster.id}").status_code == 401


# ---------------------------------------------------------------- clip.title


def test_clip_title_round_trip(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    created = client.post(
        f"/api/v1/episodes/{world['episode'].id}/clips",
        json={"duration_s": "5.00", "title": "  荒诞接生与功德圆满  "},
    )
    assert created.status_code == 201
    assert created.json()["title"] == "荒诞接生与功德圆满"
    clip_id = created.json()["id"]

    patched = client.patch(
        f"/api/v1/clips/{clip_id}",
        json={"title": "开场：猪窝接生"},
        headers={"If-Match": '"1"'},
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "开场：猪窝接生"

    listed = client.get(f"/api/v1/episodes/{world['episode'].id}/clips").json()
    assert {clip["id"]: clip["title"] for clip in listed}[clip_id] == "开场：猪窝接生"


def test_clip_title_is_optional(client: TestClient, world: dict, settings: Settings, session: Session) -> None:
    """已存在的片段标题可以为 null（draft 阶段还没起名）。"""
    clip = session.get(Clip, world["clip"].id)
    assert clip is not None
    clip.title = None
    session.flush()
    login(client, world["owner"], settings)
    assert client.get(f"/api/v1/clips/{clip.id}").json()["title"] is None


def test_clip_title_length_limit(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = f"/api/v1/episodes/{world['episode'].id}/clips"
    too_long = client.post(url, json={"duration_s": "5.00", "title": "长" * 201})
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "VALIDATION_RANGE"
    blank = client.post(url, json={"duration_s": "5.00", "title": "   "})
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "VALIDATION_SHAPE"


def test_slot_script_ref_round_trips(
    client: TestClient, world: dict, settings: Settings, poster: Asset
) -> None:
    """槽位 JSON 多了 `script_ref` 后，回读校验（`extra="forbid"`）必须放行 —— 否则读接口 500。"""
    login(client, world["owner"], settings)
    patched = client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        json={
            "ref_slots": [
                {
                    "idx": 1,
                    "kind": "image",
                    "asset_id": poster.id,
                    "script_ref": "01场景-荒郊猪窝-泥潭夜景",
                    "role": "实景",
                }
            ]
        },
        headers={"If-Match": '"1"'},
    )
    assert patched.status_code == 200
    slot = client.get(f"/api/v1/clips/{world['clip'].id}").json()["ref_slots"][0]
    assert slot["asset_id"] == poster.id
    assert slot["script_ref"] == "01场景-荒郊猪窝-泥潭夜景"


def poster_url(asset: Asset) -> str:
    return f"/api/v1/assets/{asset.id}/file"
