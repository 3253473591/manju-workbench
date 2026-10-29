"""真 provider 适配层与 `/admin/providers`。

用 `httpx.MockTransport` 造上游：形状按 OpenAI 的 `/chat/completions`，验的是**适配层自己的
契约** —— 用量怎么记、错误怎么分类（retryable 与否）、模型编出来的 verdict 怎么过滤。
没有真 key 也要能把这些钉住，否则接上真 key 那天才发现解析写错就晚了。
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from manju import providers as provider_lib
from manju.config import Settings, load_settings, provider_problems
from manju.providers import OpenAICompatProvider, ProviderError, make_provider
from tests.conftest import login


def _client(handler) -> OpenAICompatProvider:
    transport = httpx.MockTransport(handler)
    return OpenAICompatProvider(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        text_model="text-model",
        vision_model="vision-model",
        client_factory=lambda timeout=None: httpx.Client(transport=transport, timeout=timeout),
    )


def _ok(content: str, *, prompt: int = 120, completion: int = 40) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
    )


# ---------------------------------------------------------------- text


def test_text_parses_payload_and_usage() -> None:
    provider = _client(lambda request: _ok('{"prompt":"镜头1（0-6秒）：远景急推"}'))
    result = provider.text(stage="S4", context="…")
    assert result.payload == {"prompt": "镜头1（0-6秒）：远景急推"}
    assert result.input_tokens == 120 and result.output_tokens == 40
    assert result.model == "text-model"
    assert result.cost == Decimal(0)  # 文本单价未定：记 0，不编价格


def test_text_strips_markdown_fence() -> None:
    provider = _client(lambda request: _ok('```json\n{"logline":"一句概述"}\n```'))
    assert provider.text(stage="S1", context="…").payload == {"logline": "一句概述"}


def test_text_rejects_non_json_output() -> None:
    provider = _client(lambda request: _ok("好的，我来写一段剧本"))
    with pytest.raises(ProviderError) as caught:
        provider.text(stage="S2", context="…")
    assert caught.value.retryable is False


def test_text_requests_json_mode() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _ok('{"ok":true}')

    _client(handler).text(stage="S1", context="…")
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["model"] == "text-model"


# ---------------------------------------------------------------- 错误分类


@pytest.mark.parametrize(
    "status,retryable",
    [(401, False), (403, False), (429, True), (500, True), (503, True), (400, False)],
)
def test_http_errors_are_classified(status: int, retryable: bool) -> None:
    provider = _client(lambda request: httpx.Response(status, text="nope"))
    with pytest.raises(ProviderError) as caught:
        provider.text(stage="S1", context="…")
    assert caught.value.retryable is retryable


def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as caught:
        _client(handler).text(stage="S1", context="…")
    assert caught.value.retryable is True


def test_retry_after_is_carried() -> None:
    provider = _client(lambda request: httpx.Response(429, headers={"retry-after": "7"}, text="slow down"))
    with pytest.raises(ProviderError) as caught:
        provider.text(stage="S1", context="…")
    assert caught.value.raw.get("retry_after") == "7"


# ---------------------------------------------------------------- vision


def test_vision_filters_out_of_protocol_verdicts(tmp_path: Path) -> None:
    image = tmp_path / "sheet.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    content = json.dumps(
        {
            "verdicts": [
                {"id": "a1", "result": "pass", "evidence": "光环可见"},
                {"id": "a2", "result": "maybe", "evidence": "模型编的值"},
                {"id": "a3", "result": "unknown"},
                {"result": "pass"},
                "垃圾条目",
            ]
        }
    )
    provider = _client(lambda request: _ok(content))
    result = provider.vision(image=image, criteria=[{"id": "a1", "criterion": "功德环在画"}])
    assert result.verdicts == [
        {"id": "a1", "result": "pass", "evidence": "光环可见"},
        {"id": "a3", "result": "unknown", "evidence": ""},
    ]


def test_vision_sends_data_url(tmp_path: Path) -> None:
    image = tmp_path / "sheet.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _ok('{"verdicts":[]}')

    _client(handler).vision(image=image, criteria=[{"id": "a1", "criterion": "x"}])
    parts = seen["messages"][1]["content"]
    assert any(part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/jpeg;base64,") for part in parts)


# ---------------------------------------------------------------- video 明确不做


def test_video_is_explicitly_not_implemented() -> None:
    provider = _client(lambda request: _ok("{}"))
    with pytest.raises(ProviderError) as caught:
        provider.video(prompt="p", seconds=Decimal("6.00"), model="whatever")
    assert caught.value.retryable is False
    assert "视频通道尚未接入" in str(caught.value)
    assert provider.estimate(Decimal("6.00")) == Decimal(0)


# ---------------------------------------------------------------- 注册表与配置校验


def test_make_provider_registry(tmp_path: Path) -> None:
    settings = Settings(
        database_url="mysql+pymysql://root:root@127.0.0.1:3306/x", jwt_secret="x" * 32,
        csrf_origins=("http://x",), media_root=tmp_path,
    )
    assert make_provider(settings, "mock").name == "mock"
    real = make_provider(
        Settings(**{**settings.__dict__, "provider": "openai", "provider_base_url": "https://x/v1",
                    "provider_api_key": "k", "provider_text_model": "m"}),
        "openai",
    )
    assert real.name == "openai"
    with pytest.raises(ValueError):
        make_provider(settings, "seedance")


def test_provider_problems_only_bite_for_real_providers() -> None:
    base = dict(
        database_url="x", jwt_secret="x" * 32, csrf_origins=("http://x",), media_root=Path("."),
    )
    assert provider_problems(Settings(**base)) == []  # 默认 mock：零 key 起得来
    missing = provider_problems(Settings(**base, provider="openai"))
    assert len(missing) == 3  # base_url / api_key / text_model 各一条
    assert provider_problems(Settings(**base, provider="seedance"))[0].startswith("PROVIDER=seedance")


def test_load_settings_reads_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER", "openai")
    monkeypatch.setenv("PROVIDER_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("PROVIDER_API_KEY", "sk-1")
    monkeypatch.setenv("PROVIDER_TEXT_MODEL", "text-model")
    settings = load_settings()
    assert settings.provider == "openai"
    assert settings.provider_base_url.endswith("/v1")
    assert provider_problems(settings) == []


# ---------------------------------------------------------------- /admin/providers


def test_admin_panel_requires_admin(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    assert client.get("/api/v1/admin/providers").status_code == 403


def test_admin_panel_shows_mock_capabilities(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["admin"], settings)
    body = client.get("/api/v1/admin/providers").json()
    assert body["provider"] == "mock"
    assert body["text"]["real"] is False and body["video"]["real"] is False
    assert "环境变量" in body["note"]

    checked = client.post("/api/v1/admin/providers/check").json()
    assert checked["ok"] is True and checked["provider"] == "mock"
    assert "mock 模式" in checked["detail"]


def test_provider_status_reports_real_channels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = Settings(
        database_url="x", jwt_secret="x" * 32, csrf_origins=("http://x",), media_root=tmp_path,
        provider="openai", provider_base_url="https://api.example.com/v1", provider_api_key="sk-1",
        provider_text_model="text-model", provider_vision_model="vision-model",
    )
    status = provider_lib.provider_status(settings)
    assert status["text"]["real"] is True and status["vision"]["real"] is True
    assert status["video"]["real"] is False  # 没配 arrow：视频通道仍是 mock


# ---------------------------------------------------------------- Ark（Seedance 真出片）


def _ark(handler, tmp_path: Path, **kwargs) -> provider_lib.ArkVideoProvider:
    transport = httpx.MockTransport(handler)
    return provider_lib.ArkVideoProvider(
        base_url="https://ark.example.com/api/v3",
        api_key="ark-test",
        model="ep-1",
        media_root=tmp_path,
        poll_interval=0,
        client_factory=lambda timeout=None: httpx.Client(transport=transport, timeout=timeout),
        **kwargs,
    )


def test_ark_video_happy_path_records_token_cost(tmp_path: Path) -> None:
    """真口径：上游按 token 计费、产物是签名直链、时长分辨率从任务详情回读。"""
    task = {"status": "running"}
    created: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            created.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "cgt-1"})
        if request.url.path.endswith("/tasks/cgt-1"):
            task.update(
                {"status": "succeeded", "model": "doubao-seedance-2-0-mini-260615",
                 "duration": 4, "resolution": "480p",
                 "content": {"video_url": "https://cdn.example.com/v.mp4"},
                 "usage": {"completion_tokens": 40594, "total_tokens": 40594}}
            )
            return httpx.Response(200, json=task)
        return httpx.Response(200, content=b"\x00" * 2000)  # 下载

    provider = _ark(handler, tmp_path)
    result = provider.video(prompt="雨夜猪窝", seconds=Decimal("4.00"), model="ep-1")

    assert "雨夜猪窝 --resolution 720p --duration 4" in created["content"][0]["text"]
    assert result.cost == (Decimal(40594) / 1000 * Decimal("0.008727")).quantize(Decimal("0.000001"))
    assert result.unit_price is None  # 按 token 计费，不造元/秒的伪单价
    assert result.input_tokens == 40594
    assert result.video_seconds == Decimal("4")
    assert result.model.startswith("doubao-seedance")
    written = tmp_path / result.output_path
    assert written.is_file() and written.stat().st_size == 2000


def test_ark_task_failure_is_not_retryable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "cgt-2"})
        return httpx.Response(
            200, json={"status": "failed", "error": {"message": "内容审核未通过"}}
        )

    with pytest.raises(ProviderError) as caught:
        _ark(handler, tmp_path).video(prompt="p", seconds=Decimal("4.00"), model="ep-1")
    assert caught.value.retryable is False
    assert "内容审核未通过" in str(caught.value)


def test_ark_create_rejection_is_not_retryable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"error":{"code":"InvalidParameter"}}')

    with pytest.raises(ProviderError) as caught:
        _ark(handler, tmp_path).video(prompt="p", seconds=Decimal("4.00"), model="ep-1")
    assert caught.value.retryable is False


def test_ark_estimate_uses_measured_tokens_per_second(tmp_path: Path) -> None:
    provider = _ark(lambda request: httpx.Response(200, json={}), tmp_path)
    # 720p 常数 21,675 tokens/秒 × 4 s ÷ 1000 × 8.727e-3 元/千token ≈ 0.7566 元
    assert provider.estimate(Decimal("4.00")) == Decimal("0.756631")


def test_ark_text_and_vision_are_rejected(tmp_path: Path) -> None:
    provider = _ark(lambda request: httpx.Response(200, json={}), tmp_path)
    with pytest.raises(ProviderError):
        provider.text(stage="S4", context="…")
    with pytest.raises(ProviderError):
        provider.vision(image=tmp_path / "x.jpg", criteria=[])


def test_video_provider_problems(tmp_path: Path) -> None:
    base = dict(database_url="x", jwt_secret="x" * 32, csrf_origins=("http://x",), media_root=tmp_path)
    assert provider_problems(Settings(**base, provider_video="ark")) != []  # 缺 key 与推理点
    ok = Settings(
        **base, provider_video="ark", provider_video_api_key="ark-1", provider_video_model="ep-1"
    )
    assert provider_problems(ok) == []
    assert provider_problems(Settings(**base, provider_video="kling"))[0].startswith("PROVIDER_VIDEO=kling")


# ---------------------------------------------------------------- 真产物入仓要补元信息


def test_enrich_asset_fills_size_duration_and_thumb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真 provider 出片 → 入仓时没有宽高/时长/海报。没有这一步，镜头墙取图 404（实测）。"""
    from manju import seed
    from manju.models import Asset

    media = tmp_path / ".gen"
    media.mkdir(parents=True)
    (media / "cgt-1.mp4").write_bytes(b"\x00" * 100)
    asset = Asset(
        project_id=1, kind="video", path=".gen/cgt-1.mp4", sha256="a" * 64, bytes=100, source="gen",
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    monkeypatch.setattr(seed, "_probe_size", lambda source: (864, 496))
    monkeypatch.setattr(seed, "_probe_duration", lambda source: Decimal("4.10"))
    monkeypatch.setattr(
        seed, "poster",
        lambda media_root, asset, source: seed.Poster(
            relative=".thumbs/x.jpg", width=864, height=496, thumb_w=720, thumb_h=413
        ),
    )
    seed.enrich_asset(asset, tmp_path)
    assert (asset.w, asset.h) == (864, 496)
    assert asset.dur_s == Decimal("4.10")
    assert asset.thumb_path == ".thumbs/x.jpg"
    assert (asset.thumb_w, asset.thumb_h) == (720, 413)


def test_enrich_asset_is_safe_without_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from manju import seed
    from manju.models import Asset

    (tmp_path / "a.mp4").write_bytes(b"\x00")
    asset = Asset(
        project_id=1, kind="video", path="a.mp4", sha256="b" * 64, bytes=1, source="gen",
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    monkeypatch.setattr(seed, "_probe_size", lambda source: None)
    monkeypatch.setattr(seed, "_probe_duration", lambda source: None)
    monkeypatch.setattr(seed, "poster", lambda media_root, asset, source: None)
    seed.enrich_asset(asset, tmp_path)  # 不抛
    assert asset.thumb_path is None and asset.w is None


# ---------------------------------------------------------------- 出图通道（images）


def test_images_provider_b64_and_url_paths(tmp_path: Path) -> None:
    """兼容 `b64_json` 与 `url` 两种返回（文档里两种都写了）。"""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    seen: dict = {}

    def b64_handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"model": "gpt-image-2", "data": [{"b64_json": base64.b64encode(png).decode()}]})

    provider = _images(b64_handler, tmp_path)
    result = provider.image(prompt="写实人物立绘，修仙师父")
    assert seen["size"] == "1024x1024" and seen["quality"] == "low" and seen["n"] == 1
    assert result.source == "gen"
    assert (tmp_path / result.path).read_bytes() == png
    assert result.bytes == len(png)

    def url_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/a.png"}]})
        return httpx.Response(200, content=png)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(provider_lib.httpx, "get", lambda url, timeout=None: httpx.Response(200, content=png))
    try:
        second = _images(url_handler, tmp_path).image(prompt="x")
    finally:
        monkeypatch.undo()
    assert (tmp_path / second.path).read_bytes() == png


def test_images_provider_error_classification(tmp_path: Path) -> None:
    for status, retryable in [(401, False), (429, True), (500, True), (400, False)]:
        with pytest.raises(ProviderError) as caught:
            _images(lambda request, s=status: httpx.Response(s, text="x"), tmp_path).image(prompt="p")
        assert caught.value.retryable is retryable, status


def test_images_provider_rejects_other_capabilities(tmp_path: Path) -> None:
    provider = _images(lambda request: httpx.Response(200, json={}), tmp_path)
    with pytest.raises(ProviderError):
        provider.text(stage="S4", context="…")
    with pytest.raises(ProviderError):
        provider.video(prompt="p", seconds=Decimal("4.00"), model="m")


def test_mock_image_replays_existing_asset(tmp_path: Path) -> None:
    """mock 出图 = 复制已有素材，不画假图、也不假装模型生成了。"""
    folder = tmp_path / "00_静态资源"
    folder.mkdir()
    (folder / "00人物-师父-立绘.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 32)
    provider = provider_lib.MockProvider(None, media_root=tmp_path)
    result = provider.image(prompt="师父立绘")
    assert result.source == "mock" and result.cost == Decimal(0)
    assert (tmp_path / result.path).is_file()
    assert result.raw["replay"] == "00人物-师父-立绘.png"


def test_mock_image_without_reference_is_clear_error(tmp_path: Path) -> None:
    provider = provider_lib.MockProvider(None, media_root=tmp_path)
    with pytest.raises(ProviderError) as caught:
        provider.image(prompt="x")
    assert caught.value.retryable is False


def test_image_channel_problems(tmp_path: Path) -> None:
    base = dict(database_url="x", jwt_secret="x" * 32, csrf_origins=("http://x",), media_root=tmp_path)
    assert provider_problems(Settings(**base, provider_image="images")) != []
    ok = Settings(
        **base, provider_image="images", provider_image_base_url="https://ai.example.com/v1",
        provider_image_api_key="k", provider_image_model="gpt-image-2",
    )
    assert provider_problems(ok) == []
    assert provider_lib.provider_status(ok)["image"]["real"] is True


def _images(handler, tmp_path: Path, **kwargs) -> provider_lib.ImagesProvider:
    transport = httpx.MockTransport(handler)
    return provider_lib.ImagesProvider(
        base_url="https://ai.example.com/v1",
        api_key="k",
        model="gpt-image-2",
        media_root=tmp_path,
        client_factory=lambda timeout=None: httpx.Client(transport=transport, timeout=timeout),
        **kwargs,
    )
