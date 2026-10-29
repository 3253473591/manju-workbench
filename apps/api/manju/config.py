"""运行期配置与启动校验（`03` §12.1：宁可起不来，不要带病跑）。

配置只从环境变量读，不引 pydantic-settings —— 需要校验的项少，一个 `config_problems()`
比一层 Settings 类更直白。

provider 相关项分两组：
- **选 `mock`（默认）时一个都不用配**，零 key 能跑完整演示；
- 选了真 provider 就必须配齐 `PROVIDER_BASE_URL` / `PROVIDER_API_KEY` / `PROVIDER_TEXT_MODEL`，
  缺任何一项启动即失败（`03` §12.1）。价格与容量表仍不在这里：它们属于 provider 路由表配置
  （`01` §3.8），等 `/admin/providers` 落地后从那里读。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from manju.db import DEFAULT_URL

COOKIE_NAME = "dsh_session"


def _int_env(name: str, default: int) -> int:
    """读整数环境变量。配错要**说清是哪个变量**，而不是抛一个裸 ValueError traceback。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数，当前值 {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    csrf_origins: tuple[str, ...]
    media_root: Path
    cookie_secure: bool = False
    jwt_ttl: int = 7 * 24 * 3600
    db_echo: bool = False
    # 后台 worker：进程内的 asyncio 轮询（`02` §3.5 的 queued → running）。
    # 测试里关掉，改成直接调 `jobs.run_pending` —— 否则异步时序会让断言不稳定。
    worker: bool = True
    # 内容级验收点是否交视觉模型判。关掉就保持 unknown（不猜），链路照跑。
    vision: bool = True
    # ---- provider：`mock`（零成本回放）或 `openai`（任何 OpenAI 兼容的 /chat/completions）----
    provider: str = "mock"
    provider_base_url: str = ""
    provider_api_key: str = ""
    provider_text_model: str = ""
    provider_vision_model: str = ""
    provider_video_model: str = ""
    provider_timeout: int = 120
    # ---- 视频通道：`mock`（回放已有成片）或 `ark`（火山方舟 Seedance，真出片）----
    provider_video: str = "mock"
    provider_video_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    provider_video_api_key: str = ""
    provider_video_resolution: str = "720p"
    # 真视频按 token 计费（实测 4 s/480p = 40,594 tokens）。默认值由项目自己的实测常数反推：
    # 260,100 tokens ≈ 2.27 元 → 8.73e-6 元/token。换厂商/档位就改这里。
    provider_video_price_per_1k_tokens: str = "0.008727"
    # ---- 出图通道：`mock`（复制已有素材）或 `images`（OpenAI Images 兼容 /v1/images/generations）----
    provider_image: str = "mock"
    provider_image_base_url: str = ""
    provider_image_api_key: str = ""
    provider_image_model: str = "gpt-image-2"
    provider_image_size: str = "1024x1024"
    provider_image_quality: str = "low"
    provider_image_price_per_image: str = "0"


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", DEFAULT_URL),
        jwt_secret=os.environ.get("JWT_SECRET", ""),
        csrf_origins=tuple(part.strip() for part in os.environ.get("CSRF_ORIGINS", "").split(",") if part.strip()),
        media_root=Path(os.environ.get("MEDIA_ROOT", ".")).resolve(),
        cookie_secure=os.environ.get("COOKIE_SECURE", "0") == "1",
        db_echo=os.environ.get("DB_ECHO", "0") == "1",
        worker=os.environ.get("WORKER", "1") == "1",
        vision=os.environ.get("VISION", "1") == "1",
        provider=os.environ.get("PROVIDER", "mock").strip().lower(),
        provider_base_url=os.environ.get("PROVIDER_BASE_URL", "").strip(),
        provider_api_key=os.environ.get("PROVIDER_API_KEY", "").strip(),
        provider_text_model=os.environ.get("PROVIDER_TEXT_MODEL", "").strip(),
        provider_vision_model=os.environ.get("PROVIDER_VISION_MODEL", "").strip(),
        provider_video_model=os.environ.get("PROVIDER_VIDEO_MODEL", "").strip(),
        provider_video=os.environ.get("PROVIDER_VIDEO", "mock").strip().lower(),
        provider_video_base_url=os.environ.get(
            "PROVIDER_VIDEO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
        ).strip(),
        provider_video_api_key=os.environ.get("PROVIDER_VIDEO_API_KEY", "").strip(),
        provider_video_resolution=os.environ.get("PROVIDER_VIDEO_RESOLUTION", "720p").strip(),
        provider_video_price_per_1k_tokens=os.environ.get(
            "PROVIDER_VIDEO_PRICE_PER_1K_TOKENS", "0.008727"
        ).strip(),
        provider_image=os.environ.get("PROVIDER_IMAGE", "mock").strip().lower(),
        provider_image_base_url=os.environ.get("PROVIDER_IMAGE_BASE_URL", "").strip(),
        provider_image_api_key=os.environ.get("PROVIDER_IMAGE_API_KEY", "").strip(),
        provider_image_model=os.environ.get("PROVIDER_IMAGE_MODEL", "gpt-image-2").strip(),
        provider_image_size=os.environ.get("PROVIDER_IMAGE_SIZE", "1024x1024").strip(),
        provider_image_quality=os.environ.get("PROVIDER_IMAGE_QUALITY", "low").strip(),
        provider_image_price_per_image=os.environ.get("PROVIDER_IMAGE_PRICE_PER_IMAGE", "0").strip(),
        provider_timeout=_int_env("PROVIDER_TIMEOUT", 120),
    )


def provider_problems(settings: Settings) -> list[str]:
    """选真 provider 时必须配齐的东西。`mock` 一律不拦 —— 开源仓库要能零 key 起。"""
    problems: list[str] = []
    if settings.provider != "mock":
        if settings.provider != "openai":
            problems.append(f"PROVIDER={settings.provider} 不认识（可选：mock / openai）")
        else:
            if not settings.provider_base_url.startswith(("http://", "https://")):
                problems.append("PROVIDER_BASE_URL 缺失或不是 http(s) 地址")
            if not settings.provider_api_key:
                problems.append("PROVIDER_API_KEY 缺失（真 provider 必须有 key）")
            if not settings.provider_text_model:
                problems.append("PROVIDER_TEXT_MODEL 缺失（S1~S5 用它）")
    if settings.provider_video != "mock":
        if settings.provider_video != "ark":
            problems.append(f"PROVIDER_VIDEO={settings.provider_video} 不认识（可选：mock / ark）")
        else:
            if not settings.provider_video_base_url.startswith(("http://", "https://")):
                problems.append("PROVIDER_VIDEO_BASE_URL 缺失或不是 http(s) 地址")
            if not settings.provider_video_api_key:
                problems.append("PROVIDER_VIDEO_API_KEY 缺失（Ark 的 ark-… key）")
            if not settings.provider_video_model.startswith("ep-"):
                problems.append("PROVIDER_VIDEO_MODEL 缺失或不是推理点 id（ep-…）")
            try:
                if Decimal(settings.provider_video_price_per_1k_tokens) <= 0:
                    problems.append("PROVIDER_VIDEO_PRICE_PER_1K_TOKENS 必须为正数")
            except InvalidOperation:
                problems.append("PROVIDER_VIDEO_PRICE_PER_1K_TOKENS 不是合法数字")
    if settings.provider_image != "mock":
        if settings.provider_image != "images":
            problems.append(f"PROVIDER_IMAGE={settings.provider_image} 不认识（可选：mock / images）")
        else:
            if not settings.provider_image_base_url.startswith(("http://", "https://")):
                problems.append("PROVIDER_IMAGE_BASE_URL 缺失或不是 http(s) 地址")
            if not settings.provider_image_api_key:
                problems.append("PROVIDER_IMAGE_API_KEY 缺失")
            if not settings.provider_image_model:
                problems.append("PROVIDER_IMAGE_MODEL 缺失")
    return problems


def config_problems(settings: Settings) -> list[str]:
    """返回缺失项；非空就 exit 1。启动期发现的代价远低于运行时。"""
    problems = []
    if len(settings.jwt_secret) < 32:
        problems.append("JWT_SECRET 缺失或短于 32 字符")
    if not settings.csrf_origins:
        problems.append("CSRF_ORIGINS 为空（生产会全员写请求 403，且看不出原因）")
    if not settings.media_root.is_dir():
        problems.append(f"MEDIA_ROOT 不是目录：{settings.media_root}")
    elif not os.access(settings.media_root, os.W_OK):
        problems.append(f"MEDIA_ROOT 不可写：{settings.media_root}")
    problems.extend(provider_problems(settings))
    return problems
