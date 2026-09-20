"""媒体落盘与交付核验。

设计要点：
- 每个物理文件一行 `media_items`，`file_key` 是相对 MEDIA_ROOT 的键，不用可重复的业务名。
- 供应商成功 ≠ 交付成功：这里负责「落盘、可定位、可解码」，并把结论写进 delivery_records。
- ffmpeg / ffprobe 一律用文件传参、stdout 走 DEVNULL，**不做管道捕获**（沙箱禁止管道）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from app.core.config import settings
from app.core.errors import AppError
from app.core.utils import new_ulid
from app.models.enums import DecodeStatus, MediaType
from app.models.media import MediaItem

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


# --- 路径 ---------------------------------------------------------------


def media_root() -> Path:
    root = settings.media_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def path_for(file_key: str) -> Path:
    return media_root() / file_key


def new_file_key(suffix: str, *, media_type: MediaType = MediaType.image) -> str:
    now = datetime.now(timezone.utc)
    folder = f"{media_type.value}/{now:%Y/%m}"
    return f"{folder}/{new_ulid()}{suffix}"


def thumbnail_key_for(file_key: str) -> str:
    path = Path(file_key)
    return str(path.with_suffix("")) + ".thumb.jpg"


# --- 基础工具 -----------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_bytes(data: bytes, *, media_type: MediaType, suffix: str) -> tuple[str, str, int]:
    file_key = new_file_key(suffix, media_type=media_type)
    target = path_for(file_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return file_key, hashlib.sha256(data).hexdigest(), len(data)


def copy_file(source: Path, *, media_type: MediaType, suffix: str | None = None) -> tuple[str, str, int]:
    suffix = suffix or source.suffix or ".bin"
    file_key = new_file_key(suffix, media_type=media_type)
    target = path_for(file_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return file_key, sha256_file(target), target.stat().st_size


def detect_media_type(mime_type: str, suffix: str) -> MediaType:
    if mime_type.startswith("image/") or suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".gif",
    }:
        return MediaType.image
    if mime_type.startswith("video/") or suffix.lower() in {".mp4", ".mov", ".webm", ".m4v"}:
        return MediaType.video
    if mime_type.startswith("audio/"):
        return MediaType.audio
    return MediaType.file


# --- 探测 ---------------------------------------------------------------


def probe_image(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.size
    except Exception:  # noqa: BLE001 - 任何解码失败都算不可用
        return None


def probe_video(path: Path) -> dict | None:
    """用 ffprobe 读流信息。

    结果让 ffprobe 直接写到工作区内的文件，再读回来；**不用 mkstemp**（它返回的句柄
    会让文件在 Windows 上无法删除），也不做管道输出捕获。
    """
    temp_path = media_root() / f"{new_ulid()}.ffprobe.json"
    try:
        result = subprocess.run(  # noqa: S603
            [
                FFPROBE,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                "-o",
                str(temp_path),
                str(path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0 or not temp_path.exists():
            return None
        payload = json.loads(temp_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 探测失败就是不可用
        return None
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    video_stream = next(
        (s for s in payload.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        return None
    duration = payload.get("format", {}).get("duration") or video_stream.get("duration")
    return {
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "duration_ms": int(float(duration) * 1000) if duration else 0,
    }


def write_thumbnail(source: Path, target: Path, *, max_edge: int | None = None) -> bool:
    """图片缩略图；视频取第一帧。失败返回 False（不掩盖为成功）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    edge = max_edge or settings.THUMBNAIL_MAX_EDGE
    if source.suffix.lower() in {".mp4", ".mov", ".webm", ".m4v"}:
        result = subprocess.run(  # noqa: S603
            [
                FFMPEG,
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({edge},iw)':-2",
                str(target),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0 and target.exists()
    try:
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((edge, edge))
            image.save(target, format="JPEG", quality=82)
        return True
    except Exception:  # noqa: BLE001
        return False


# --- 交付核验 -----------------------------------------------------------


def verify_media_item(db, item: MediaItem) -> MediaItem:  # type: ignore[no-untyped-def]
    """落盘 → 定位 → 解码。三步都过才标记 verified。"""
    path = path_for(item.file_key)
    if not path.exists() or path.stat().st_size == 0:
        item.decode_status = DecodeStatus.failed
        db.add(item)
        return item

    if item.media_type == MediaType.image:
        size = probe_image(path)
        if size is None:
            item.decode_status = DecodeStatus.failed
            db.add(item)
            return item
        item.width, item.height = size
    elif item.media_type == MediaType.video:
        info = probe_video(path)
        if info is None:
            item.decode_status = DecodeStatus.failed
            db.add(item)
            return item
        item.width = info["width"] or None
        item.height = info["height"] or None
        item.duration_ms = info["duration_ms"] or None
    else:
        # 文本类附件只校验可定位、非空
        pass

    item.decode_status = DecodeStatus.verified
    item.sha256 = item.sha256 or sha256_file(path)
    db.add(item)
    return item


def ensure_thumbnail(item: MediaItem) -> str | None:
    """生成缩略图；视频取第一帧。返回缩略图 file_key 或 None。"""
    source = path_for(item.file_key)
    if not source.exists():
        return None
    target_key = thumbnail_key_for(item.file_key)
    target = path_for(target_key)
    if target.exists():
        return target_key
    if write_thumbnail(source, target):
        return target_key
    return None


def register_media_item(
    db,  # type: ignore[no-untyped-def]
    *,
    data: bytes,
    media_type: MediaType,
    suffix: str,
    original_name: str = "",
    mime_type: str = "",
    created_by: str | None = None,
    parsed_kind: str = "",
    parsed_name: str = "",
    parsed_variant: str = "",
) -> MediaItem:
    file_key, digest, size = save_bytes(data, media_type=media_type, suffix=suffix)
    item = MediaItem(
        media_type=media_type,
        file_key=file_key,
        original_name=original_name,
        mime_type=mime_type,
        size_bytes=size,
        sha256=digest,
        decode_status=DecodeStatus.pending,
        parsed_kind=parsed_kind,
        parsed_name=parsed_name,
        parsed_variant=parsed_variant,
        created_by=created_by,
    )
    db.add(item)
    db.flush()
    verify_media_item(db, item)
    ensure_thumbnail(item)
    db.flush()
    return item


def require_decodable(item: MediaItem | None) -> MediaItem:
    if item is None:
        raise AppError("DELIVERY_FAILED", "媒体记录缺失")
    if item.decode_status != DecodeStatus.verified:
        raise AppError(
            "DELIVERY_FAILED",
            "媒体文件未通过交付核验",
            details={"mediaItemId": item.id, "decodeStatus": item.decode_status.value},
        )
    return item
