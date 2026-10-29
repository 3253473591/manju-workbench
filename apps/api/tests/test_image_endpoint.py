"""出图端点：产物入仓与去重（P0-3 剩下的那一块）。

两个只在**真链路**里才会暴露的点，各自有用例：
1. mock 出图不要复制文件 —— 入仓按 sha256 去重会命中原素材，复制只会留孤儿文件；
2. 响应里要能分清"资产来源"（可能命中 seed/upload）与"这次是不是真出图"（`provider_source`）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import providers as provider_lib
from manju.config import Settings
from manju.models import Asset
from tests.conftest import login


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def media(media_root: Path) -> Path:
    folder = media_root / "00_静态资源"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "00人物-师父-立绘.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 32)
    return media_root


def _mock_url(monkeypatch: pytest.MonkeyPatch, media: Path) -> None:
    """把 `PROVIDER_IMAGE=mock` 下的 provider 指到测试用的 media_root。"""
    monkeypatch.setattr(
        provider_lib,
        "make_image_provider",
        lambda settings: provider_lib.MockProvider(None, media_root=settings.media_root),
    )


def test_mock_image_dedupes_and_leaves_no_orphan(
    client: TestClient, session: Session, world: dict, settings: Settings, media: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_url(monkeypatch, media)
    login(client, world["owner"], settings)

    first = client.post(f"/api/v1/projects/{world['project'].id}/images", json={"prompt": "师父立绘"})
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["provider_source"] == "mock"
    assert body["deduped"] is False
    assert body["source"] == "seed"  # 数据模型只认 gen/upload/seed；真假看 provider_source
    # mock 不复制文件：资产直接指向原素材
    assert body["path"].startswith("00_静态资源/")

    second = client.post(f"/api/v1/projects/{world['project'].id}/images", json={"prompt": "换个说法"})
    assert second.status_code == 201
    assert second.json()["asset_id"] == body["asset_id"]  # 同 sha 命中同一条资产
    assert second.json()["deduped"] is True

    total = session.scalars(select(Asset).where(Asset.project_id == world["project"].id)).all()
    assert len(total) == 1
    # 不产生任何派生文件
    assert list((media / ".gen").glob("mock-img-*")) == []


def test_image_endpoint_reports_real_provider_source(
    client: TestClient, session: Session, world: dict, settings: Settings, media: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真出图：source=gen、deduped=False，并且入仓时补了宽高与海报。"""
    from manju.providers import ImageResult

    png = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64

    class FakeImages(provider_lib.Provider):
        name = "fake-images"

        def image(self, *, prompt: str, model: str | None = None) -> ImageResult:
            folder = settings.media_root / ".gen"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "img-abc.png").write_bytes(png)
            return ImageResult(
                model="gpt-image-2", path=".gen/img-abc.png", bytes=len(png), width=1024, height=1024,
                cost=Decimal("0.1"), latency_ms=42, source="gen",
            )

    monkeypatch.setattr("manju.api.images.make_image_provider", lambda settings: FakeImages())
    monkeypatch.setattr("manju.api.images.enrich_asset", lambda asset, root: setattr(asset, "thumb_path", ".thumbs/x.jpg"))
    login(client, world["owner"], settings)

    response = client.post(f"/api/v1/projects/{world['project'].id}/images", json={"prompt": "p"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["provider_source"] == "gen" and body["deduped"] is False
    assert body["source"] == "gen" and body["cost"] == "0.1"
    asset = session.get(Asset, body["asset_id"])
    assert asset.thumb_path == ".thumbs/x.jpg"


def test_image_endpoint_requires_write(
    client: TestClient, world: dict, settings: Settings, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_url(monkeypatch, media)
    login(client, world["viewer"], settings)
    assert client.post(f"/api/v1/projects/{world['project'].id}/images", json={"prompt": "p"}).status_code == 403


def test_image_endpoint_reports_provider_error(
    client: TestClient, world: dict, settings: Settings, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Boom(provider_lib.Provider):
        name = "boom"

        def image(self, *, prompt: str, model: str | None = None):
            raise provider_lib.ProviderError("上游 500", retryable=True, status=500)

    monkeypatch.setattr("manju.api.images.make_image_provider", lambda settings: Boom())
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/projects/{world['project'].id}/images", json={"prompt": "p"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"

