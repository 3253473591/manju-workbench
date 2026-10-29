"""竞态与非原子操作的硬化（读-写插入、配置解析）。

这些缺陷的共同点：**窗口小、正常测不出来**，只有并发或配错时才炸，而且炸出来是 500。
所以用例不追求"真的并发两次"（不稳定），而是**人为把窗口撑开**：
第一次查询假装查不到（= 竞态窗口），插入必然撞唯一键，看服务端是否回读对手那一行。

同时钉住"配错要说清是哪个变量"，而不是抛一个裸 ValueError traceback。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from manju import jobs
from manju.api import images as images_api
from manju.config import Settings, load_settings
from manju.models import Asset, Clip
from manju.providers import ImageResult, Provider
from tests.conftest import login


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------- 配置解析


def test_bad_int_env_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER_TIMEOUT", "十分钟")
    with pytest.raises(ValueError) as caught:
        load_settings()
    message = str(caught.value)
    assert "PROVIDER_TIMEOUT" in message and "十分钟" in message


def test_empty_int_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER_TIMEOUT", "")
    assert load_settings().provider_timeout == 120


# ---------------------------------------------------------------- 出图入仓竞态


class _FixedImage(Provider):
    name = "fixed-images"

    def __init__(self, payload: bytes, path: str) -> None:
        self.payload = payload
        self.path = path

    def image(self, *, prompt: str, model: str | None = None) -> ImageResult:
        return ImageResult(
            model="gpt-image-2", path=self.path, bytes=len(self.payload), width=None, height=None,
            cost=Decimal("0.1"), latency_ms=1, source="gen",
        )


def test_image_insert_race_falls_back_to_existing_asset(
    client: TestClient, session: Session, world: dict, settings: Settings, media_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """竞败路径：第一次"查同 sha"落空 → 插入撞 uk_asset_sha → 回读对手行，返回 201 而不是 500。"""
    payload = b"\x89PNG\r\n\x1a\n" + b"\x07" * 32
    digest = hashlib.sha256(payload).hexdigest()
    (media_root / ".gen").mkdir(parents=True, exist_ok=True)
    (media_root / ".gen" / "img-race.png").write_bytes(payload)

    # 对手先入仓（手工造行，模拟并发赢家）
    winner = Asset(
        project_id=world["project"].id, kind="image", path=".gen/img-race.png", sha256=digest,
        bytes=len(payload), source="gen", created_at=_now(),
    )
    session.add(winner)
    session.flush()

    monkeypatch.setattr("manju.api.images.make_image_provider", lambda settings: _FixedImage(payload, ".gen/img-race.png"))
    monkeypatch.setattr("manju.api.images.enrich_asset", lambda asset, root: None)

    calls = {"count": 0}
    real_select = images_api.select

    def flaky_select(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:  # 模拟竞态窗口：这一眼没看到对手刚插的行
            return real_select(Asset).where(Asset.id == -1)
        return real_select(*args, **kwargs)

    monkeypatch.setattr("manju.api.images.select", flaky_select)
    login(client, world["owner"], settings)

    response = client.post(f"/api/v1/projects/{world['project'].id}/images", json={"prompt": "p"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["asset_id"] == winner.id
    assert body["deduped"] is True


# ---------------------------------------------------------------- 产物入仓竞态


def test_video_output_insert_race_falls_back_to_existing_asset(
    session: Session, world: dict, settings: Settings, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\x00" * 64
    (media_root / ".gen").mkdir(parents=True, exist_ok=True)
    (media_root / ".gen" / "cgt-race.mp4").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    winner = Asset(
        project_id=world["project"].id, kind="video", path=".gen/cgt-race.mp4", sha256=digest,
        bytes=len(payload), source="gen", created_at=_now(),
    )
    session.add(winner)
    session.flush()

    clip = session.get(Clip, world["clip"].id)
    monkeypatch.setattr(jobs.seed, "enrich_asset", lambda asset, root: None)

    calls = {"seen": False}
    real_select = jobs.select

    def flaky_select(*args, **kwargs):
        statement = str(args[0]) if args else ""
        # 只拦截"查资产"的那一条（`_project_id` 也走 jobs.select，不能一起劫持）
        if "asset.sha256" in statement and not calls["seen"]:
            calls["seen"] = True
            return real_select(Asset).where(Asset.id == -1)
        return real_select(*args, **kwargs)

    monkeypatch.setattr(jobs, "select", flaky_select)
    variant = jobs._resolve_output(session, settings, clip, ".gen/cgt-race.mp4")
    assert variant.id == winner.id
