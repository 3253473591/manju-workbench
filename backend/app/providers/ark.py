"""火山方舟（Ark）真实适配器。

合同来源：API对接文档.md。只接官方接口，不接任何第三方中转。
密钥只从环境变量读取（通过 ModelConfig.credential_ref 指向的变量名），
绝不写入代码、日志、响应或输入快照。
"""

from __future__ import annotations

import base64
import mimetypes
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import new_ulid
from app.providers.base import (
    ImageProvider,
    MediaResult,
    ProviderError,
    ProviderUnavailable,
    TextProvider,
    TextResult,
    VideoProvider,
)


def resolve_credential(credential_ref: str) -> str:
    """按环境变量名取密钥；取不到就明确报不可用，不用空串硬发。

    `ARK_SEEDREAM_LITE_API_KEY` 这类分档位变量为空时，回退到同能力共用的
    `ARK_SEEDREAM_API_KEY`，兼容「两档一把 Key」的部署方式。
    """
    if not credential_ref:
        raise ProviderUnavailable("模型配置缺少 credential_ref，无法解析 API Key")
    value = (getattr(settings, credential_ref, "") or "").strip()
    if value:
        return value

    fallback_map = {
        "ARK_SEEDREAM_LITE_API_KEY": "ARK_SEEDREAM_API_KEY",
        "ARK_SEEDREAM_PRO_API_KEY": "ARK_SEEDREAM_API_KEY",
    }
    fallback_ref = fallback_map.get(credential_ref)
    if fallback_ref:
        fallback = (getattr(settings, fallback_ref, "") or "").strip()
        if fallback:
            return fallback
    raise ProviderUnavailable(f"环境变量 {credential_ref} 未配置，无法调用真实模型")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.ARK_BASE_URL,
        timeout=httpx.Timeout(settings.ARK_TIMEOUT_SECONDS, connect=30.0),
    )


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    detail = ""
    try:
        payload = response.json()
        detail = str(payload.get("error") or payload)[:500]
    except ValueError:
        detail = response.text[:500]
    if response.status_code == 429:
        raise ProviderError(f"供应商限流：{detail}", code="RATE_LIMITED")
    if response.status_code >= 500:
        raise ProviderError(f"供应商不可用：{detail}", code="PROVIDER_UNAVAILABLE")
    raise ProviderError(f"供应商拒绝请求：{detail}", code="PROVIDER_REJECTED")


class ArkTextProvider(TextProvider):
    """豆包文本与提示词。结构化任务优先请求 JSON 模式，服务端仍需 schema 校验。"""

    name = "ark-text"

    def __init__(self, *, credential_ref: str = "ARK_DOUBAO_API_KEY", actual_model: str = "") -> None:
        self.credential_ref = credential_ref
        self.actual_model = actual_model

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        expect: str = "text",
        json_mode: bool = True,
        context: dict[str, Any] | None = None,
    ) -> TextResult:
        api_key = resolve_credential(self.credential_ref)
        body: dict[str, Any] = {
            "model": settings.ARK_DOUBAO_MODEL,
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
            ],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        with _client() as client:
            response = client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
        _raise_for_status(response)
        payload = response.json()

        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("供应商响应缺少 choices", code="PROVIDER_REJECTED")
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, list):
            text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        else:
            text = content or ""
        if not text.strip():
            raise ProviderError("供应商返回空正文", code="PROVIDER_REJECTED")

        usage = payload.get("usage") or {}
        return TextResult(
            text=text,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            provider_request_id=response.headers.get("x-request-id", "") or f"ark_req_{new_ulid()}",
            raw_meta={"model": payload.get("model", "")},
        )


class ArkImageProvider(ImageProvider):
    """Seedream 文生图 / 单源图生图。"""

    name = "ark-image"

    def __init__(
        self,
        *,
        credential_ref: str = "ARK_SEEDREAM_API_KEY",
        endpoint_id: str = "",
        min_pixels: int = 3686400,
    ) -> None:
        self.credential_ref = credential_ref
        self.endpoint_id = endpoint_id
        self.min_pixels = min_pixels

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
        api_key = resolve_credential(self.credential_ref)
        _assert_min_pixels(size, self.min_pixels)

        body: dict[str, Any] = {
            "model": self.endpoint_id or settings.ARK_SEEDREAM_LITE_MODEL,
            "prompt": prompt,
            "size": size,
            "n": count,
            # 实测用的就是 url；服务端立刻下载并落盘，不把 URL 当作交付完成
            "response_format": "url",
            "watermark": False,
        }
        if source_image is not None:
            body["image"] = _to_data_url(source_image, source_mime)
        if seed >= 0:
            body["seed"] = seed

        with _client() as client:
            response = client.post(
                "/images/generations",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            _raise_for_status(response)
            payload = response.json()
            items = payload.get("data") or []
            if not items:
                raise ProviderError("响应没有 data 产物", code="PROVIDER_REJECTED")
            first = items[0]
            if first.get("b64_json"):
                data = base64.b64decode(first["b64_json"])
                mime, suffix = "image/png", ".png"
            elif first.get("url"):
                download = client.get(first["url"])
                download.raise_for_status()
                data = download.content
                mime = download.headers.get("content-type", "image/jpeg").split(";")[0]
                suffix = mimetypes.guess_extension(mime) or ".jpg"
            else:
                raise ProviderError("产物既没有 url 也没有 b64_json", code="PROVIDER_REJECTED")

        usage = payload.get("usage") or {}
        return MediaResult(
            data=data,
            mime_type=mime,
            suffix=suffix,
            usage=usage,
            provider_request_id=response.headers.get("x-request-id", ""),
            actual_model=payload.get("model", ""),
            raw_meta={"usage": usage, "created": payload.get("created")},
        )


def _assert_min_pixels(size: str, min_pixels: int) -> None:
    """1024x1024 被官方明确拒绝过；提交前校验，避免无效计费。"""
    try:
        width, height = (int(part) for part in size.lower().split("x"))
    except (ValueError, AttributeError) as exc:
        raise ProviderError(f"图片尺寸格式非法：{size}", code="INVALID_REQUEST") from exc
    if width * height < min_pixels:
        raise ProviderError(
            f"图片尺寸 {size} 不满足最小面积 {min_pixels} 像素（官方实测拒绝 1024x1024）",
            code="MODEL_NOT_AVAILABLE",
        )


def _to_data_url(data: bytes, mime: str) -> str:
    return f"data:{mime or 'image/png'};base64," + base64.b64encode(data).decode()


class ArkVideoProvider(VideoProvider):
    """Seedance 图生视频：创建任务 → 轮询 → 下载。

    attempt 的物理状态推进：reserved → accepted → running → succeeded/failed/unknown。
    查询不新增 attempt；下载失败只重试下载，不重新计费生成。
    """

    name = "ark-video"

    def __init__(
        self,
        *,
        credential_ref: str = "ARK_SEEDANCE_MINI_API_KEY",
        endpoint_id: str = "",
        on_accepted: Any = None,
    ) -> None:
        self.credential_ref = credential_ref
        self.endpoint_id = endpoint_id
        #: 回调：拿到供应商任务 ID 后立刻持久化，避免响应丢失后无法恢复
        self.on_accepted = on_accepted

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
        api_key = resolve_credential(self.credential_ref)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in reference_images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _to_data_url(image, "image/jpeg")},
                    "role": "reference_image",
                }
            )
        body: dict[str, Any] = {
            "model": self.endpoint_id or settings.ARK_SEEDANCE_MINI_MODEL,
            "content": content,
            "resolution": resolution,
            "ratio": ratio,
            "duration": duration_seconds,
            "camera_fixed": False,
            "watermark": False,
            "seed": seed,
        }

        with _client() as client:
            created = client.post(
                "/contents/generations/tasks",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            _raise_for_status(created)
            created_payload = created.json()
            task_id = created_payload.get("id")
            if not task_id:
                # 没有有效 ID 不得标为已成功创建
                raise ProviderError("创建任务未返回有效 id", code="PROVIDER_TIMEOUT_UNKNOWN")
            if self.on_accepted is not None:
                self.on_accepted(task_id)

            deadline = time.monotonic() + settings.ARK_POLL_MAX_SECONDS
            task_payload: dict[str, Any] = created_payload
            while True:
                status = str(task_payload.get("status", "")).lower()
                if status == "succeeded":
                    break
                if status in {"failed", "cancelled", "expired"}:
                    raise ProviderError(
                        f"供应商任务结束于 {status}：{task_payload.get('error')}",
                        code="PROVIDER_REJECTED",
                    )
                if time.monotonic() > deadline:
                    raise ProviderError("等待供应商任务超时", code="PROVIDER_TIMEOUT_UNKNOWN")
                time.sleep(settings.ARK_POLL_INTERVAL_SECONDS)
                polled = client.get(
                    f"/contents/generations/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                _raise_for_status(polled)
                task_payload = polled.json()

            video_url = (task_payload.get("content") or {}).get("video_url")
            if not video_url:
                raise ProviderError("任务成功但没有 video_url", code="DELIVERY_FAILED")
            download = client.get(video_url)
            download.raise_for_status()
            data = download.content

        usage = task_payload.get("usage") or {}
        return MediaResult(
            data=data,
            mime_type="video/mp4",
            suffix=".mp4",
            usage=usage,
            provider_request_id=created.headers.get("x-request-id", ""),
            provider_task_id=task_id,
            actual_model=task_payload.get("model", ""),
            raw_meta={"status": task_payload.get("status"), "usage": usage},
        )
