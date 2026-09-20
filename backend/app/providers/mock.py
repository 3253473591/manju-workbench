"""mock 供应商：不发任何外部模型请求，但产出**真实可用**的本地结果。

- 文本：按任务结构返回可解析的 JSON，保证下游链路能真正跑通。
- 图片：真的用 Pillow 画一张 PNG，交付核验会真解码。
- 视频：真的用 ffmpeg 生成可播放的 MP4，首帧缩略图也真提取。

这样 PRD §9 的 mock 验收（含「视频落盘后可播放并展示首帧」）才是真的通过，
而不是拿占位符冒充。
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from app.core.utils import new_ulid
from app.providers.base import (
    ImageProvider,
    MediaResult,
    ProviderUnavailable,
    TextProvider,
    TextResult,
    VideoProvider,
)

MOCK_PALETTE = [
    ((245, 158, 11), (251, 113, 133)),
    ((56, 189, 248), (30, 64, 175)),
    ((52, 211, 153), (5, 150, 105)),
    ((167, 139, 250), (76, 29, 149)),
]


def _seed_from(*parts: object) -> int:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8], 16)


class MockTextProvider(TextProvider):
    name = "mock-text"

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        expect: str = "text",
        json_mode: bool = True,
        context: dict[str, Any] | None = None,
    ) -> TextResult:
        context = context or {}
        rng = random.Random(_seed_from(user_prompt, expect, context.get("salt")))
        payload = _build_payload(expect, context, rng)
        text = json.dumps(payload, ensure_ascii=False)
        # 用量是模拟值，但结构与真实回执一致，便于前端与费用链路统一处理
        return TextResult(
            text=text,
            input_tokens=len(system_prompt) + len(user_prompt),
            output_tokens=len(text),
            provider_request_id=f"mock_req_{new_ulid()}",
            raw_meta={"mock": True, "expect": expect},
        )


def _build_payload(expect: str, context: dict[str, Any], rng: random.Random) -> dict:
    subject = context.get("subject") or "林晚"
    location = context.get("location") or "雨夜校门"

    if expect == "setting":
        names = context.get("characters") or [subject, "沈亦舟"]
        return {
            "characters": [
                {
                    "name": name,
                    "identity": "高三学生" if index == 0 else "转学生",
                    "personality": "外冷内热，嘴上不饶人",
                    "appearance": "清瘦，眼神很静",
                    "hair": "黑色长发，银色发带",
                    "baseOutfit": "深藏青校服外套，白衬衫",
                    "outfitVariants": "冬装：加米色围巾；雨夜：深色连帽外套",
                    "signatureProps": "一把透明长柄伞",
                }
                for index, name in enumerate(names)
            ]
        }

    if expect == "clips":
        count = int(context.get("clipCount") or 3)
        clips = []
        for index in range(count):
            clip_no = index + 1
            shots = []
            for shot_index in range(2):
                start = shot_index * 2500
                shots.append(
                    {
                        "shotNo": shot_index + 1,
                        "startMs": start,
                        "endMs": start + 2400,
                        "visualText": f"{location}，雨幕中的第 {clip_no} 场第 {shot_index + 1} 镜",
                        "cameraText": "中景推近" if shot_index == 0 else "过肩反打",
                        "dialogueText": f"（第 {clip_no} 场）你终于回来了。",
                        "soundText": "雨声，远处车流",
                    }
                )
            clips.append(
                {
                    "clipNo": clip_no,
                    "title": ["雨夜重逢", "伞下沉默", "灯光熄灭"][index % 3],
                    "durationMs": 5000 + index * 500,
                    "characters": [subject],
                    "locationName": location,
                    "shots": shots,
                }
            )
        return {"clips": clips}

    if expect == "assets":
        asset_type = context.get("assetType") or "character"
        clip_ids = context.get("clipIds") or []
        if asset_type == "character":
            items = [
                {
                    "assetType": "character",
                    "canonicalName": subject,
                    "variantName": "",
                    "imageKind": "portrait",
                    "clipIds": clip_ids,
                }
            ]
        elif asset_type == "scene":
            items = [
                {
                    "assetType": "scene",
                    "canonicalName": location,
                    "variantName": "",
                    "imageKind": "scene",
                    "clipIds": clip_ids,
                }
            ]
        else:
            items = [
                {
                    "assetType": "prop",
                    "canonicalName": "透明长柄伞",
                    "variantName": "",
                    "imageKind": "prop",
                    "clipIds": clip_ids,
                }
            ]
        return {"assets": items}

    if expect == "video_prompt":
        return {"visibleText": _build_video_prompt(context)}

    if expect == "visible_text":
        base = context.get("base") or "雨夜校门口，男主回头"
        return {
            "visibleText": f"{base}；{location}，雨丝逆光，伞面泛着冷光，情绪克制"
        }

    if expect == "clip_optimize":
        return {
            "title": context.get("title") or "雨夜重逢",
            "body": "雨幕压低了整条街，他停在伞沿三步之外，没有开口。",
            "visual": "雨幕中的校门口，男主回头",
            "camera": "中景缓慢推近至近景",
            "dialogue": "你终于回来了。",
        }

    # 默认：普通文本正文
    return {
        "title": context.get("title") or "两个校花女友",
        "body": (
            f"{subject}在{location}等了三十分钟。\n"
            "伞面上的雨声越来越密，她终于看见那个熟悉的身影从雨里走出来。\n"
            "「你终于回来了。」\n"
            "他没有回答，只是把手里的伞往她那边倾了倾。\n"
        ),
    }


_CN_NUMBERS = "一二三四五六七八九十"


def _cn(index: int) -> str:
    return _CN_NUMBERS[index - 1] if 1 <= index <= 10 else str(index)


def _build_video_prompt(context: dict[str, Any]) -> str:
    """按 Seedance 2.0 终剪交付物的结构产出可用的 mock 提示词。

    结构照抄真实交付物（见 AI漫剧/剧情/…/Seedance2.0终剪提示词.md）：
    参考图定义段 → 视角段 → `镜头N（起-止秒）` 分镜段 → 环境音段 → 质量收尾句。
    这样 mock 路径产出的提示词能直接通过 prompt_rules 的产线规范校验。
    """
    duration = max(4, int(context.get("durationSeconds") or 5))
    title = str(context.get("clipTitle") or "片段")
    location = str(context.get("location") or "雨夜校门")
    names = [str(name) for name in (context.get("referenceNames") or [])] or ["角色立绘"]
    mid = max(1, round(duration / 2))

    lines: list[str] = []
    for index, name in enumerate(names, start=1):
        if index == 1:
            lines.append(
                f"将参考图{_cn(index)}中的{location}定义为场景：{location}，"
                "湿石板与雨幕反光，环境光影全片统一。"
            )
        elif index == 2:
            lines.append(
                f"参考图{_cn(index)}为{name}的服装形制锚点：款式与配色一经确认，"
                "全链路沿用同一套写法，服装形制写死到鞋。"
            )
        else:
            lines.append(
                f"将参考图{_cn(index)}中的{name}定义为本片参考主体："
                "外形锚点与体量保持不变，作为造型与身份的判断依据。"
            )

    lines.append("")
    lines.append("全片为第三人称叙述视角，一镜到底、不切镜、不反打。")
    lines.append("")
    lines.append(
        f"镜头1（0-{mid}秒）：{title}起手，人物立于画面中央，镜头缓慢推近，雨丝逆光。"
    )
    lines.append(
        f"镜头2（{mid}-{duration}秒）：动作延续到收束，镜头横移跟随，情绪收在最后一拍。"
    )
    lines.append("")
    lines.append("环境音：雨声、脚步声与衣料摩擦声，不生成背景音乐。")
    lines.append("")
    lines.append(
        "画面高清，电影质感，柔和侧光与雨幕反光；人物面部稳定不变形，动作自然流畅，"
        "无卡顿无闪烁；无字幕，避免生成任何文字或字幕。"
    )
    return "\n".join(lines)


class MockImageProvider(ImageProvider):
    name = "mock-image"

    def generate(
        self,
        *,
        prompt: str,
        size: str,
        count: int = 1,
        seed: int = -1,
        source_image: bytes | None = None,
        source_mime: str = "image/png",
        product_label: str = "",
    ) -> MediaResult:
        width, height = _parse_size(size)
        data = _render_png(prompt, width, height, seed if seed >= 0 else _seed_from(prompt))
        return MediaResult(
            data=data,
            mime_type="image/png",
            suffix=".png",
            usage={"input_images": 1 if source_image else 0, "generated_images": 1},
            provider_request_id=f"mock_req_{new_ulid()}",
            actual_model="mock-image-1",
            raw_meta={"mock": True, "size": size, "promptHash": hashlib.sha256(prompt.encode()).hexdigest()[:16]},
        )


def _parse_size(size: str) -> tuple[int, int]:
    try:
        width, height = size.lower().split("x")
        return int(width), int(height)
    except (ValueError, AttributeError):
        return 1024, 1024


def _render_png(prompt: str, width: int, height: int, seed: int) -> bytes:
    rng = random.Random(seed)
    start, end = MOCK_PALETTE[seed % len(MOCK_PALETTE)]
    image = Image.new("RGB", (width, height), start)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(
            int(start[i] + (end[i] - start[i]) * ratio) for i in range(3)
        )
        draw.line([(0, y), (width, y)], fill=color)
    # 画几个几何块，让不同 prompt 的产物肉眼可区分
    for _ in range(6):
        x0 = rng.randint(0, max(width - 80, 1))
        y0 = rng.randint(0, max(height - 80, 1))
        x1 = min(x0 + rng.randint(60, 260), width - 1)
        y1 = min(y0 + rng.randint(60, 260), height - 1)
        box = (x0, y0, x1, y1)
        draw.rectangle(box, outline=(255, 255, 255), width=max(width // 240, 1))
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    draw.text((16, 16), f"MOCK {digest}", fill=(255, 255, 255))
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class MockVideoProvider(VideoProvider):
    name = "mock-video"

    def generate(
        self,
        *,
        prompt: str,
        reference_images: list[bytes],
        duration_seconds: int = 5,
        resolution: str = "720p",
        ratio: str = "16:9",
        seed: int = -1,
        product_label: str = "",
    ) -> MediaResult:
        width, height = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}.get(
            resolution, (1280, 720)
        )
        duration = max(1, min(int(duration_seconds or 5), 30))
        frame = reference_images[0] if reference_images else None
        if frame is None:
            frame = _render_png(prompt, width, height, _seed_from(prompt))
        data = _render_mp4(frame, width, height, duration)
        return MediaResult(
            data=data,
            mime_type="video/mp4",
            suffix=".mp4",
            usage={
                "reference_images": len(reference_images),
                "completion_tokens": 21690 * duration,
            },
            provider_request_id=f"mock_req_{new_ulid()}",
            actual_model="mock-video-1",
            raw_meta={"mock": True, "resolution": resolution, "ratio": ratio, "duration": duration},
        )


def _render_mp4(frame_bytes: bytes, width: int, height: int, duration: int) -> bytes:
    """用 ffmpeg 把一帧图渲染成真实可播放的 MP4。stdout/stderr 走 DEVNULL，不做管道捕获。

    临时目录必须放在工作区内：系统 %TEMP% 在受限沙箱下不可写，用默认目录会直接 PermissionError。
    """
    from app.core.config import settings

    work_root = settings.media_root / "tmp"
    work_root.mkdir(parents=True, exist_ok=True)
    # 不用 mkdtemp/TemporaryDirectory：
    # 1) Windows 上 ffmpeg 退出后句柄可能没释放，自动清理会抛 PermissionError；
    # 2) 系统 %TEMP% 在受限沙箱下不可写。
    # 这里用普通 mkdir 建一个唯一子目录，最后忽略错误地清理。
    work_dir = work_root / new_ulid()
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        src = work_dir / "frame.png"
        dst = work_dir / "out.mp4"
        with Image.open(__import__("io").BytesIO(frame_bytes)) as image:
            image.convert("RGB").resize((width, height)).save(src, format="PNG")
        result = subprocess.run(  # noqa: S603
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-loop",
                "1",
                "-i",
                str(src),
                "-t",
                str(duration),
                "-r",
                "24",
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                "-c:v",
                "libx264",
                "-movflags",
                "+faststart",
                str(dst),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0 or not dst.exists():
            raise ProviderUnavailable(
                "mock 视频生成失败：ffmpeg 不可用或编码失败（mock 也要求产出真实可播放的 MP4）"
            )
        return dst.read_bytes()
    finally:
        import shutil as _shutil

        _shutil.rmtree(work_dir, ignore_errors=True)


__all__ = ["MockImageProvider", "MockTextProvider", "MockVideoProvider"]
