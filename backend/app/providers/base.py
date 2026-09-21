"""供应商适配器接口。

适配器只返回规范化结果，**不直接写节点、费用或画布**（domain-boundaries §4）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextResult:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_request_id: str = ""
    provider_task_id: str | None = None
    raw_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaResult:
    data: bytes
    mime_type: str
    suffix: str
    usage: dict[str, Any] = field(default_factory=dict)
    provider_request_id: str = ""
    provider_task_id: str | None = None
    actual_model: str = ""
    raw_meta: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """供应商明确拒绝或不可用。"""

    def __init__(self, message: str, *, code: str = "PROVIDER_REJECTED") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProviderUnavailable(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="PROVIDER_UNAVAILABLE")


class TextProvider(ABC):
    name: str = "text"

    @abstractmethod
    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        expect: str = "text",
        json_mode: bool = True,
        context: dict[str, Any] | None = None,
    ) -> TextResult: ...


class ImageProvider(ABC):
    name: str = "image"

    @abstractmethod
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
    ) -> MediaResult: ...


class VideoProvider(ABC):
    name: str = "video"

    @abstractmethod
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
    ) -> MediaResult: ...

    def resume(self, *, task_id: str, product_label: str = "") -> MediaResult:
        """按**已存在**的供应商任务 ID 恢复查询与下载。

        这是「供应商超时后不重复计费」的关键：恢复路径只允许查询既有任务，
        绝不能重新提交。不支持恢复查询的适配器必须明确报不可用，而不是
        悄悄再建一个任务。
        """
        raise ProviderUnavailable(
            f"{self.name} 适配器不支持按任务 ID 恢复查询"
        )
