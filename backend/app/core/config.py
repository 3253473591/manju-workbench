"""应用配置。所有密钥只从环境变量读取，不写入代码库。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 运行模式 -------------------------------------------------------
    # mock：不发任何外部模型请求，产出可查看的本地示例结果
    # real：走火山方舟官方接口
    APP_MODE: str = "mock"
    APP_NAME: str = "漫剧工作台"
    API_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # --- 数据库 ---------------------------------------------------------
    DATABASE_URL: str = (
        "mysql+pymysql://root:root@127.0.0.1:3306/manju_director?charset=utf8mb4"
    )
    DATABASE_AUTO_CREATE: bool = True
    DB_ECHO: bool = False

    # --- 会话 -----------------------------------------------------------
    SESSION_TTL_HOURS: int = 24 * 14
    BOOTSTRAP_ADMIN_USERNAME: str = "admin"
    BOOTSTRAP_ADMIN_PASSWORD: str = "admin123"
    BOOTSTRAP_ADMIN_DISPLAY_NAME: str = "超级管理员"
    PASSWORD_MIN_LENGTH: int = 6

    # --- 媒体落盘 -------------------------------------------------------
    MEDIA_ROOT: str = str(PROJECT_DIR / "var" / "media")
    MAX_UPLOAD_BYTES: int = 64 * 1024 * 1024
    THUMBNAIL_MAX_EDGE: int = 640

    # --- 后台 worker ----------------------------------------------------
    WORKER_ENABLED: bool = True
    WORKER_INTERVAL_SECONDS: float = 1.0
    # A lease prevents a crashed worker from permanently locking a node.
    GENERATION_LEASE_SECONDS: int = 1800
    # 供应商任务恢复：accepted/running/unknown 的 attempt 按 provider_task_id 复查。
    # 复查间隔与最大次数共同保证「不重复计费」且「不会永久卡住」。
    GENERATION_RECONCILE_INTERVAL_SECONDS: int = 30
    GENERATION_RECONCILE_MAX_ATTEMPTS: int = 20

    # --- 火山方舟（只接官方）--------------------------------------------
    ARK_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"
    ARK_TIMEOUT_SECONDS: float = 600.0
    ARK_POLL_INTERVAL_SECONDS: float = 10.0
    ARK_POLL_MAX_SECONDS: float = 1800.0

    ARK_DOUBAO_API_KEY: str = ""
    ARK_DOUBAO_MODEL: str = "ep-20260917112408-5v5pr"

    # Seedream lite / pro 是两把不同的 Key，按档位各自解析；
    # ARK_SEEDREAM_API_KEY 作为两档共用一个 Key 时的回退。
    ARK_SEEDREAM_API_KEY: str = ""
    ARK_SEEDREAM_LITE_API_KEY: str = ""
    ARK_SEEDREAM_PRO_API_KEY: str = ""
    ARK_SEEDREAM_LITE_MODEL: str = "ep-20260920164208-q8z6z"
    ARK_SEEDREAM_PRO_MODEL: str = "ep-20260920164144-9g86q"

    ARK_SEEDANCE_MINI_API_KEY: str = ""
    ARK_SEEDANCE_MINI_MODEL: str = "ep-20260911123900-jzrcv"
    ARK_SEEDANCE_FAST_API_KEY: str = ""
    ARK_SEEDANCE_FAST_MODEL: str = "ep-20260911124121-s98bf"
    ARK_SEEDANCE_25_API_KEY: str = ""
    ARK_SEEDANCE_25_MODEL: str = "ep-20260911124159-mkx2n"

    # --- CORS -----------------------------------------------------------
    CORS_ORIGINS: str = "http://127.0.0.1:5173,http://localhost:5173"

    @property
    def is_mock(self) -> bool:
        return self.APP_MODE.strip().lower() != "real"

    @property
    def media_root(self) -> Path:
        return Path(self.MEDIA_ROOT)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
