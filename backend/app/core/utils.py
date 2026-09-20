"""ULID 主键与 UTC 时间工具。不引第三方依赖。"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MAX_TIME_MS = (1 << 48) - 1


def _encode(value: int, length: int) -> str:
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(chars)


def new_ulid() -> str:
    """26 字符 Crockford Base32 ULID，按时间单调可排序。"""
    ms = int(time.time() * 1000) & _MAX_TIME_MS
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(ms, 10) + _encode(randomness, 16)


def utcnow() -> datetime:
    """朴素 UTC 时间；数据库统一存 UTC，API 侧再补时区。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")
