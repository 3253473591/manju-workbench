"""资产删除与"被引用即禁删"（`01` §4.5 / `02` §4 的 `CONFLICT_ASSET_REFERENCED`）。

三条规矩，各自有用例：
1. **被引用就 409**，并把引用方（哪个集、哪个片段、哪个槽位 / 哪次 take）列出来 ——
   让删的人知道该先改哪儿，而不是只看到一句"不能删"；
2. **不做级联**：悬空槽位比"删不掉"更难查；
3. **物理文件默认不删**：`MEDIA_ROOT` 是用户自己的剧集目录，种子/上传素材删了没有第二份；
   `?purge=1` 只清 `.gen/` 下我们自己生成的派生文件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Asset, Clip, Project, Revision
from tests.conftest import login, make_user


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _asset(session: Session, world: dict, *, path: str = "第一集/a.png", source: str = "seed") -> Asset:
    asset = Asset(
        project_id=world["project"].id, kind="image", path=path, sha256=f"{abs(hash(path)):064d}",
        bytes=4, source=source, created_at=_now(),
    )
    session.add(asset)
    session.flush()
    return asset


def _clip_with_slot(session: Session, world: dict, asset: Asset, *, idx: int = 8) -> Clip:
    clip = Clip(
        episode_id=world["episode"].id, idx=idx, source_idx=idx, duration_s=Decimal("6.00"), status="draft",
        ref_slots_json=[{"idx": 1, "kind": "image", "asset_id": asset.id, "asset_ref": "a", "role": "实景", "note": ""}],
        created_at=_now(), updated_at=_now(),
    )
    session.add(clip)
    session.flush()
    return clip


def test_delete_unreferenced_asset(client: TestClient, session: Session, world: dict, settings: Settings) -> None:
    asset = _asset(session, world)
    login(client, world["owner"], settings)
    assert client.delete(f"/api/v1/assets/{asset.id}").status_code == 204
    assert client.get(f"/api/v1/assets/{asset.id}").status_code == 404


def test_delete_blocked_when_referenced_by_slot(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    asset = _asset(session, world)
    clip = _clip_with_slot(session, world, asset)
    login(client, world["owner"], settings)

    response = client.delete(f"/api/v1/assets/{asset.id}")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT_ASSET_REFERENCED"
    usages = response.json()["error"]["detail"]["usages"]
    assert usages[0]["clip_id"] == clip.id
    assert usages[0]["slot_idx"] == 1
    assert session.get(Asset, asset.id) is not None  # 没删掉


def test_delete_allowed_after_unlinking(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    asset = _asset(session, world)
    clip = _clip_with_slot(session, world, asset)
    login(client, world["owner"], settings)
    assert client.delete(f"/api/v1/assets/{asset.id}").status_code == 409

    clip.ref_slots_json = []
    session.flush()
    assert client.delete(f"/api/v1/assets/{asset.id}").status_code == 204


def test_delete_blocked_when_it_is_a_take_product(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    """产物资产是某次 take 的结果：删了会让版本链指向空，同样 409。"""
    asset = _asset(session, world, path=".gen/cgt-1.mp4", source="gen")
    revision = Revision(
        target_kind="clip", target_id=world["clip"].id, prompt="p", cost=Decimal(0),
        params_json={}, provider="ark", output_asset_id=asset.id, created_at=_now(),
    )
    session.add(revision)
    session.flush()
    login(client, world["owner"], settings)

    response = client.delete(f"/api/v1/assets/{asset.id}")
    assert response.status_code == 409
    assert response.json()["error"]["detail"]["usages"][0]["revision_id"] == revision.id


def test_delete_requires_write_and_respects_archive(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    asset = _asset(session, world)
    login(client, world["viewer"], settings)
    assert client.delete(f"/api/v1/assets/{asset.id}").status_code == 403

    login(client, world["owner"], settings)
    assert client.post(f"/api/v1/projects/{world['project'].id}/archive").status_code == 200
    assert client.delete(f"/api/v1/assets/{asset.id}").status_code == 403

    assert client.delete("/api/v1/assets/999999").status_code in (403, 404)


def test_purge_removes_only_generated_files(
    client: TestClient, session: Session, world: dict, settings: Settings, media_root: Path
) -> None:
    # 派生文件：`.gen/` 下的，purge 会删
    generated = media_root / ".gen"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "img-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    gen_asset = _asset(session, world, path=".gen/img-1.png", source="gen")

    # 种子素材：用户自己的原始文件，purge 也不删
    source = media_root / "第一集"
    source.mkdir(parents=True, exist_ok=True)
    (source / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    seed_asset = _asset(session, world, path="第一集/a.png", source="seed")

    login(client, world["owner"], settings)
    assert client.delete(f"/api/v1/assets/{gen_asset.id}?purge=1").status_code == 204
    assert not (generated / "img-1.png").exists()

    assert client.delete(f"/api/v1/assets/{seed_asset.id}?purge=1").status_code == 204
    assert (source / "a.png").exists()  # 原始素材不动


def test_asset_of_other_project_cannot_be_deleted_by_outsider(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    other = make_user(session, "other@example.com", "lead")
    foreign = Project(
        title="别人的项目", owner_id=other.id, status="active", version=1,
        created_at=_now(), updated_at=_now(),
    )
    session.add(foreign)
    session.flush()
    asset = Asset(
        project_id=foreign.id, kind="image", path="x.png", sha256="e" * 64, bytes=1, source="upload",
        created_at=_now(),
    )
    session.add(asset)
    session.flush()

    login(client, world["owner"], settings)
    # 读任意项目是产品决定，写不是：别人的资产删不了
    assert client.get(f"/api/v1/assets/{asset.id}").status_code == 200
    assert client.delete(f"/api/v1/assets/{asset.id}").status_code == 403
