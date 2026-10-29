"""引擎契约探针：钉住"我们到底发了什么、类型对不对"。

背景（wind-comic v13 的真实事故）：上游 `subject_reference[].image_file` 官方要字符串，
代码一直发数组，**每次带参考图的生成都被拒、然后静默回退到别的引擎**，直到一次 live call 才发现。

这类问题不会被单元测试里"请求成功"的断言抓到——除非断言**字段类型**。本探针：
1. 用 `httpx.MockTransport` 截获真实请求体，逐字段校验类型与必填（离线、零成本，可进 CI）；
2. 校验响应解析（token 计数、b64 落盘、错误分类）；
3. `--live` 模式对已配置的真通道打一次最小请求（显式确认才跑，视频默认跳过）。

用法：
    python scripts/contract_probe.py --offline          # CI 用；任何契约不符退出码 1
    python scripts/contract_probe.py --live --channel text --yes
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))

import httpx  # noqa: E402

from manju.providers import (  # noqa: E402
    ArkVideoProvider,
    ImagesProvider,
    OpenAICompatProvider,
    ProviderError,
)

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@dataclass
class Result:
    channel: str
    check: str
    ok: bool
    detail: str = ""


def _type_errors(value: object, expected: type | tuple[type, ...], path: str) -> list[str]:
    if not isinstance(value, expected) or isinstance(value, bool) and bool not in (
        expected if isinstance(expected, tuple) else (expected,)
    ):
        return [f"{path}: 期望 {expected}, 实际 {type(value).__name__}={value!r}"]
    return []


def check_body(body: dict, contract: dict[str, type | tuple[type, ...]]) -> list[str]:
    errors: list[str] = []
    for key, expected in contract.items():
        if key not in body:
            errors.append(f"{key}: 缺失")
            continue
        errors.extend(_type_errors(body[key], expected, key))
    return errors


def _client_factory(captured: dict, response_payload: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("calls", []).append(
            {"method": request.method, "path": request.url.path, "body": json.loads(request.content or b"{}")}
        )
        return httpx.Response(status, json=response_payload)

    transport = httpx.MockTransport(handler)
    return lambda timeout=None: httpx.Client(transport=transport, timeout=timeout)


def _chat_payload(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        "model": "probe-model",
    }


def probe_text() -> list[Result]:
    captured: dict = {}
    provider = OpenAICompatProvider(
        base_url="https://probe.invalid/v1",
        api_key="probe-key",
        text_model="probe-model",
        client_factory=_client_factory(captured, _chat_payload('{"ok": true}')),
    )
    result = provider.text(stage="S1", context="探针上下文", format_hint='{"type":"object"}')
    body = captured["calls"][0]["body"]
    errors = check_body(
        body,
        {"model": str, "messages": list, "temperature": (int, float), "response_format": dict},
    )
    errors += _type_errors(body.get("messages"), list, "messages")
    for index, message in enumerate(body.get("messages") or []):
        if not isinstance(message, dict) or not isinstance(message.get("role"), str) or not isinstance(message.get("content"), str):
            errors.append(f"messages[{index}]: 期望 role/content 都是 str")
    if (body.get("response_format") or {}).get("type") != "json_object":
        errors.append("response_format.type 期望 'json_object'")
    if captured["calls"][0]["path"] != "/v1/chat/completions":
        errors.append(f"路径期望 /v1/chat/completions，实际 {captured['calls'][0]['path']}")
    if result.input_tokens != 11 or result.output_tokens != 7:
        errors.append(f"usage 解析错：{result.input_tokens}/{result.output_tokens}")

    errors += _response_error_classification()
    return [Result("text", "请求体字段类型 + usage 解析", not errors, "; ".join(errors) or "ok")]


def _response_error_classification() -> list[str]:
    errors: list[str] = []
    for status, retryable in ((401, False), (429, True), (500, True)):
        captured: dict = {}
        provider = OpenAICompatProvider(
            base_url="https://probe.invalid/v1",
            api_key="k",
            text_model="m",
            client_factory=_client_factory(captured, {"error": "x"}, status=status),
        )
        try:
            provider.text(stage="S1", context="x")
        except ProviderError as exc:
            if exc.retryable != retryable:
                errors.append(f"HTTP {status}: retryable 期望 {retryable}，实际 {exc.retryable}")
        else:
            errors.append(f"HTTP {status}: 没有抛 ProviderError")
    return errors


def probe_vision() -> list[Result]:
    captured: dict = {}
    provider = OpenAICompatProvider(
        base_url="https://probe.invalid/v1",
        api_key="k",
        text_model="m",
        vision_model="m-vision",
        client_factory=_client_factory(captured, _chat_payload('{"verdicts": []}')),
    )
    with tempfile.TemporaryDirectory() as folder:
        image = Path(folder) / "frame.png"
        image.write_bytes(PNG_1PX)
        provider.vision(image=image, criteria=[{"id": "a1", "criterion": "画面在动"}])
    body = captured["calls"][0]["body"]
    errors = check_body(body, {"model": str, "messages": list})
    content = (body.get("messages") or [{}, {}])[1].get("content")
    if not isinstance(content, list):
        errors.append("vision messages[1].content 期望 list")
    else:
        parts = {part.get("type"): part for part in content if isinstance(part, dict)}
        if "text" not in parts or not isinstance(parts["text"].get("text"), str):
            errors.append("缺少 text part 或 text 不是 str")
        image_part = parts.get("image_url") or {}
        url = (image_part.get("image_url") or {}).get("url")
        if not isinstance(url, str) or not url.startswith("data:image/png;base64,"):
            errors.append(f"image_url.url 期望 data URL 字符串，实际 {type(url).__name__}")
    return [Result("vision", "多模态消息结构（text + image_url 字符串）", not errors, "; ".join(errors) or "ok")]


def probe_video() -> list[Result]:
    captured: dict = {}
    provider = ArkVideoProvider(
        base_url="https://probe.invalid/api/v3",
        api_key="k",
        model="ep-probe",
        resolution="480p",
        client_factory=_client_factory(captured, {"id": "cgt-probe"}),
    )
    task_id = provider._create("镜头：远景急推", Decimal("4"))
    body = captured["calls"][0]["body"]
    errors = check_body(body, {"model": str, "content": list})
    if task_id != "cgt-probe":
        errors.append(f"任务 id 解析错：{task_id}")
    content = body.get("content") or []
    if not content or not isinstance(content[0], dict):
        errors.append("content[0] 期望 dict")
    else:
        errors += _type_errors(content[0].get("type"), str, "content[0].type")
        text = content[0].get("text")
        errors += _type_errors(text, str, "content[0].text")
        if isinstance(text, str):
            for flag in ("--resolution", "--duration", "--ratio", "--watermark"):
                if flag not in text:
                    errors.append(f"提示词缺少 {flag} 参数")
            if "--duration 4 " not in text + " ":
                errors.append(f"duration 期望整数 4，实际片段：{text[:120]}")
    if captured["calls"][0]["path"] != "/api/v3/contents/generations/tasks":
        errors.append(f"路径不符：{captured['calls'][0]['path']}")
    return [Result("video", "创建任务请求体（model/content/时长参数）", not errors, "; ".join(errors) or "ok")]


def probe_image() -> list[Result]:
    captured: dict = {}
    payload = {"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
    with tempfile.TemporaryDirectory() as folder:
        media = Path(folder)
        provider = ImagesProvider(
            base_url="https://probe.invalid/v1",
            api_key="k",
            model="probe-image",
            size="1024x1024",
            quality="low",
            media_root=media,
            client_factory=_client_factory(captured, payload),
        )
        result = provider.image(prompt="一张人物立绘")
        wrote = (media / result.path).is_file()
    body = captured["calls"][0]["body"]
    errors = check_body(body, {"model": str, "prompt": str, "size": str, "quality": str, "n": int})
    if isinstance(body.get("n"), bool):
        errors.append("n 是 bool，不是 int")
    if not wrote:
        errors.append("b64_json 没有落盘")
    if body.get("n") != 1:
        errors.append(f"n 期望 1，实际 {body.get('n')!r}")

    # 响应缺 b64/url 时必须报错，而不是假装成功
    captured.clear()
    provider = ImagesProvider(
        base_url="https://probe.invalid/v1",
        api_key="k",
        model="probe-image",
        media_root=Path(tempfile.mkdtemp()),
        client_factory=_client_factory(captured, {"data": [{}]}),
    )
    try:
        provider.image(prompt="x")
    except ProviderError:
        pass
    else:
        errors.append("响应既无 b64_json 也无 url 时没有报错")
    return [Result("image", "请求体字段类型 + b64 落盘 + 空响应报错", not errors, "; ".join(errors) or "ok")]


def probe_live(channel: str) -> list[Result]:
    """对配置好的真通道打一次最小请求。只由 `--live --channel X --yes` 触发。"""
    from manju.config import load_settings
    from manju.providers import make_image_provider, make_provider

    settings = load_settings()
    if channel in ("text", "vision"):
        provider = make_provider(settings)
        if provider.name == "mock":
            return [Result(channel, "live", True, "跳过：当前是 mock")]
        if channel == "text":
            result = provider.text(stage="probe", context='只回复 JSON：{"ok": true}')
            return [Result("text", "live 文本", bool(result.payload), f"{result.latency_ms}ms / {result.output_tokens} tokens")]
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "frame.png"
            image.write_bytes(PNG_1PX)
            result = provider.vision(image=image, criteria=[{"id": "a1", "criterion": "画面里有没有人物"}])
        return [Result("vision", "live 视觉", True, f"{result.latency_ms}ms / {len(result.verdicts)} 条判定")]
    if channel == "video":
        provider = make_provider(settings, settings.provider_video)
        if provider.name == "mock":
            return [Result("video", "live", True, "跳过：当前是 mock")]
        result = provider.video(prompt="镜头：远景急推", seconds=Decimal("4"), model=settings.provider_video_model)
        return [Result("video", "live 出片（4s）", True, f"tokens={result.input_tokens} cost={result.cost}")]
    if channel == "image":
        provider = make_image_provider(settings)
        if provider.name == "mock":
            return [Result("image", "live", True, "跳过：当前是 mock")]
        result = provider.image(prompt="一张人物立绘，纯色背景")
        return [Result("image", "live 出图", True, f"{result.latency_ms}ms / {result.bytes}B")]
    return [Result(channel, "live", False, "未知通道")]


def run_offline() -> list[Result]:
    return probe_text() + probe_vision() + probe_video() + probe_image()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="离线契约校验（默认）")
    parser.add_argument("--live", action="store_true", help="真打一次上游（可能产生费用）")
    parser.add_argument("--channel", choices=("text", "vision", "video", "image"), default="text")
    parser.add_argument("--yes", action="store_true", help="确认 live 调用可能产生费用")
    args = parser.parse_args()

    results = run_offline()
    if args.live:
        if not args.yes:
            print("live 会真的调用上游并产生费用，加 --yes 确认")
            return 2
        results += probe_live(args.channel)

    width = max(len(item.check) for item in results) + 2
    for item in results:
        print(f"[{'PASS' if item.ok else 'FAIL'}] {item.channel.ljust(7)} {item.check.ljust(width)} {item.detail}")
    failed = [item for item in results if not item.ok]
    print()
    print(f"{'✅ 契约全部符合' if not failed else f'❌ {len(failed)} 项契约不符'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
