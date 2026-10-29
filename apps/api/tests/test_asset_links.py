"""资产列表、`clip.output_asset_id` 派生、以及 seed 里那两处连线（`01` §8.5）。

连线部分直接跑真实文档，验的是"剧本参考资产 → 终剪槽位"和"交付成片 → clip"这两条映射，
不是 mock。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import Asset, Clip, Revision
from manju.seed import _find_asset, poster
from tests.conftest import login

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "docs" / "我师父是个普通人"


def _seed_module():
    spec = importlib.util.spec_from_file_location("seed_project", ROOT / "scripts" / "seed_project.py")
    module = importlib.util.module_from_spec(spec)
    # 必须登记到 sys.modules：dataclass 解析字符串注解时要回查 cls.__module__ 对应的模块
    sys.modules["seed_project"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------- 资产列表


def test_asset_list_filters_and_pages(
    client: TestClient, world: dict, settings: Settings, session: Session
) -> None:
    for index, kind in enumerate(("image", "video", "image")):
        session.add(
            Asset(
                project_id=world["project"].id,
                kind=kind,
                path=f"demo/{kind}-{index}.bin",
                sha256=str(index) * 64,
                bytes=1,
                source="seed",
                created_at=_now(),
            )
        )
    session.flush()
    login(client, world["owner"], settings)

    everything = client.get(f"/api/v1/assets?project_id={world['project'].id}")
    assert everything.status_code == 200
    assert len(everything.json()["items"]) == 3

    images = client.get(f"/api/v1/assets?project_id={world['project'].id}&kind=image")
    assert images.status_code == 200
    assert {item["kind"] for item in images.json()["items"]} == {"image"}

    first = client.get(f"/api/v1/assets?project_id={world['project'].id}&limit=2")
    body = first.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None
    rest = client.get(f"/api/v1/assets?project_id={world['project'].id}&cursor={body['next_cursor']}")
    assert len(rest.json()["items"]) == 1


def test_asset_list_requires_project(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    assert client.get("/api/v1/assets").status_code == 422
    assert client.get("/api/v1/assets?project_id=999999").status_code == 404


# ---------------------------------------------------------------- output_asset_id


def test_clip_output_asset_is_derived(
    client: TestClient, world: dict, settings: Settings, session: Session
) -> None:
    video = Asset(
        project_id=world["project"].id,
        kind="video",
        path="第一集/s1.mp4",
        sha256="e" * 64,
        bytes=1,
        source="seed",
        created_at=_now(),
    )
    session.add(video)
    session.flush()
    revision = Revision(
        target_kind="clip",
        target_id=world["clip"].id,
        output_asset_id=video.id,
        cost=Decimal("2.27"),
        created_at=_now(),
    )
    session.add(revision)
    session.flush()
    clip = session.get(Clip, world["clip"].id)
    clip.current_revision_id = revision.id
    session.flush()

    login(client, world["owner"], settings)
    assert client.get(f"/api/v1/clips/{clip.id}").json()["output_asset_id"] == video.id
    listed = client.get(f"/api/v1/episodes/{clip.episode_id}/clips").json()
    assert {item["id"]: item["output_asset_id"] for item in listed}[clip.id] == video.id


def test_clip_without_revision_has_no_output(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    created = client.post(f"/api/v1/episodes/{world['episode'].id}/clips", json={"duration_s": "5.00"})
    assert created.status_code == 201
    assert client.get(f"/api/v1/clips/{created.json()['id']}").json()["output_asset_id"] is None


# ---------------------------------------------------------------- seed 连线


@pytest.fixture(scope="module")
def parsed():
    module = _seed_module()
    report = module.Report()
    script_path = SOURCE / "第一集" / "剧本.md"
    final_path = SOURCE / "第一集" / "Seedance2.0终剪提示词.md"
    if not script_path.exists() or not final_path.exists():
        pytest.skip("真实剧集目录不在工作区")
    _, script_clips = module.parse_script(script_path, report)
    clips, _ = module.parse_final_prompts(final_path, report)
    module.merge(clips, script_clips, report)
    return module, clips, report


def test_delivered_stem_follows_play_order(parsed) -> None:
    _, clips, _ = parsed
    assert [clip.delivered_stem for clip in clips] == ["s1", "s2", "s3", "s4", "s5", "s6", "s6b", "s7"]
    # 插入镜：源片段号 8、播放序 7、交付名 s6b —— 三种编号都不互相推
    inserted = clips[6]
    assert (inserted.source_idx, inserted.idx, inserted.delivered_stem) == (8, 7, "s6b")


def test_script_refs_map_to_slots(parsed) -> None:
    _, clips, _ = parsed
    first = clips[0]
    refs = {slot.idx: slot.script_ref for slot in first.ref_slots}
    assert refs[1] == "01场景-荒郊猪窝-泥潭夜景"
    assert refs[2].startswith("00人物-徒弟")
    # 剧本第 3 个资产是"系统界面"，终剪第 3 槽是母猪妖 —— 不能被序号带偏
    assert refs[3] is None
    assert refs[4] == "02道具-系统界面-造化玉碟腿毛"


def test_script_refs_cover_enough_slots(parsed) -> None:
    _, clips, report = parsed
    matched = sum(1 for clip in clips for slot in clip.ref_slots if slot.script_ref)
    total = sum(len(clip.ref_slots) for clip in clips)
    assert total == 36
    assert matched >= 17, f"只回填了 {matched}/{total} 个槽位"
    # 匹配不上必须留警告，不能静默挂错图
    assert any("留空" in item for item in report.warnings)


def test_find_asset_matches_by_stem() -> None:
    class Fake:
        def __init__(self, ident: int) -> None:
            self.id = ident
            self.kind = "image"

    scene = Fake(9)
    assets = {"00人物-徒弟-喜剧版-猪圈接生.png": Fake(7), "01场景-荒郊猪窝-泥潭夜景.png": scene}
    # 剧本里写的是不带扩展名的主干，磁盘上是同名 .png
    assert _find_asset(assets, "01场景-荒郊猪窝-泥潭夜景") is scene
    assert _find_asset(assets, "不存在的素材") is None
    assert _find_asset(assets, None) is None


# ---------------------------------------------------------------- 海报抽帧（--thumbs）


def test_poster_extracts_first_frame(media_root: Path) -> None:
    """真跑一次 ffmpeg：抽出来的必须是能服务的 jpg，而不是空文件。"""
    if shutil.which("ffmpeg") is None:
        pytest.skip("没有 ffmpeg")
    source = media_root / "demo" / "clip.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
            "-c:v", "mpeg4", str(source),
        ],
        check=True,
        capture_output=True,
    )
    asset = Asset(
        project_id=1,
        kind="video",
        path="demo/clip.mp4",
        sha256="f" * 64,
        bytes=1,
        source="seed",
        created_at=_now(),
    )
    relative = poster(media_root, asset, source)
    assert relative is not None
    assert relative.relative == ".thumbs/ffffffffffff.jpg"
    assert (relative.width, relative.height) == (320, 240)
    assert (relative.thumb_w, relative.thumb_h) == (320, 240)  # 源片小于长边上限，不放大
    thumb = media_root / relative.relative
    assert thumb.is_file()
    assert thumb.stat().st_size > 0
    assert thumb.read_bytes()[:2] == b"\xff\xd8"  # JPEG SOI


def test_poster_returns_none_on_broken_source(media_root: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("没有 ffmpeg")
    broken = media_root / "demo" / "broken.mp4"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not a video")
    asset = Asset(
        project_id=1,
        kind="video",
        path="demo/broken.mp4",
        sha256="0" * 64,
        bytes=1,
        source="seed",
        created_at=_now(),
    )
    assert poster(media_root, asset, broken) is None
