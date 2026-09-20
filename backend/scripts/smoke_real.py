"""real 模式冒烟：拿到方舟凭证后按 API对接文档 §8 跑一遍并留记录。

前提：
    set APP_MODE=real
    set ARK_DOUBAO_API_KEY=...
    set ARK_SEEDREAM_API_KEY=...
    set ARK_SEEDANCE_MINI_API_KEY=...

用法：
    run.cmd scripts\\smoke_real.py --text
    run.cmd scripts\\smoke_real.py --image
    run.cmd scripts\\smoke_real.py --video   （需要先用一个已确认参考图的 Clip 走接口链路）

不写入任何密钥；输出记录里只保留模型、耗时、usage、hash 与交付结论。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.providers.ark import (  # noqa: E402
    ArkImageProvider,
    ArkTextProvider,
    ArkVideoProvider,
)
from app.providers.base import ProviderError  # noqa: E402
from app.services import media_store  # noqa: E402


def _require(mode_ok: bool, names: list[str]) -> bool:
    if not mode_ok:
        print("拒绝执行：请先设置 APP_MODE=real")
        return False
    missing = [name for name in names if not (getattr(settings, name, "") or "").strip()]
    if missing:
        print(f"缺少环境变量：{', '.join(missing)}")
        return False
    return True


def smoke_text() -> int:
    if not _require(not settings.is_mock, ["ARK_DOUBAO_API_KEY"]):
        return 1
    provider = ArkTextProvider()
    started = time.monotonic()
    try:
        result = provider.generate(
            system_prompt="你是漫剧编剧。只输出 JSON，字段为 title 与 body。",
            user_prompt="用一句话写一个雨夜重逢的大纲。",
            expect="text",
        )
    except ProviderError as exc:
        print(f"[FAIL] {exc.code}: {exc.message}")
        return 1
    print(f"[OK] 豆包 耗时 {time.monotonic() - started:.1f}s")
    print(f"     model={result.raw_meta.get('model')}")
    print(f"     usage input={result.input_tokens} output={result.output_tokens}")
    print(f"     request_id={result.provider_request_id}")
    print(f"     content[:120]={result.text[:120]}")
    return 0


def smoke_image(source_image: Path | None = None) -> int:
    if not _require(
        not settings.is_mock,
        ["ARK_SEEDREAM_LITE_API_KEY", "ARK_SEEDREAM_PRO_API_KEY"],
    ):
        return 1
    for label, endpoint, credential in (
        ("lite", settings.ARK_SEEDREAM_LITE_MODEL, "ARK_SEEDREAM_LITE_API_KEY"),
        ("pro", settings.ARK_SEEDREAM_PRO_MODEL, "ARK_SEEDREAM_PRO_API_KEY"),
    ):
        provider = ArkImageProvider(endpoint_id=endpoint, credential_ref=credential)
        payload = {
            "prompt": "一间空的国风书房，木桌，窗边柔和日光，无人物，无可读文字",
            "size": "1920x1920",
            "count": 1,
        }
        if source_image is not None:
            payload["source_image"] = source_image.read_bytes()
            payload["prompt"] = "保持人物身份与服装结构，将背景改为干净的浅灰色"
        started = time.monotonic()
        try:
            result = provider.generate(**payload)  # type: ignore[arg-type]
        except ProviderError as exc:
            print(f"[FAIL] seedream-{label} {exc.code}: {exc.message}")
            continue
        item_key, digest, size = media_store.save_bytes(
            result.data, media_type=media_store.MediaType.image, suffix=result.suffix
        )
        print(f"[OK] seedream-{label} 耗时 {time.monotonic() - started:.1f}s")
        print(f"     actual_model={result.actual_model} usage={result.usage}")
        print(f"     file={item_key} bytes={size} sha256={digest[:16]}...")
    return 0


def smoke_video(reference: Path) -> int:
    if not _require(not settings.is_mock, ["ARK_SEEDANCE_MINI_API_KEY"]):
        return 1
    provider = ArkVideoProvider(endpoint_id=settings.ARK_SEEDANCE_MINI_MODEL)
    started = time.monotonic()

    def on_accepted(task_id: str) -> None:
        print(f"     provider_task_id={task_id}（已立刻持久化，可用于恢复查询）")

    provider.on_accepted = on_accepted
    try:
        result = provider.generate(
            prompt="参考图1：雨夜校门，男主回头。镜头1（0-5秒）缓慢推近，无字幕",
            reference_images=[reference.read_bytes()],
            duration_seconds=5,
            resolution="720p",
        )
    except ProviderError as exc:
        print(f"[FAIL] seedance {exc.code}: {exc.message}")
        return 1
    item_key, digest, size = media_store.save_bytes(
        result.data, media_type=media_store.MediaType.video, suffix=result.suffix
    )
    print(f"[OK] seedance 耗时 {time.monotonic() - started:.1f}s")
    print(f"     usage={result.usage} bytes={size} sha256={digest[:16]}...")
    # 交付核验：落盘后必须能解码并抽出首帧
    probe = media_store.probe_video(media_store.path_for(item_key))
    print(f"     probe={probe}")
    thumb = media_store.thumbnail_key_for(item_key)
    ok = media_store.write_thumbnail(
        media_store.path_for(item_key), media_store.path_for(thumb)
    )
    print(f"     first_frame_thumbnail={'ok' if ok else 'FAILED'} -> {thumb}")
    return 0 if probe and ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="real 模式冒烟")
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--image", action="store_true")
    parser.add_argument("--image-to-image", type=Path, default=None)
    parser.add_argument("--video", type=Path, default=None, help="参考图路径")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if not any([args.text, args.image, args.image_to_image, args.video, args.all]):
        parser.print_help()
        return 2

    codes = []
    if args.text or args.all:
        codes.append(smoke_text())
    if args.image or args.all:
        codes.append(smoke_image())
    if args.image_to_image:
        codes.append(smoke_image(args.image_to_image))
    if args.video:
        codes.append(smoke_video(args.video))
    return max(codes) if codes else 0


if __name__ == "__main__":
    raise SystemExit(main())
