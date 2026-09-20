"""应用入口。

启动顺序：建库 → 跑 Alembic 迁移 → seed 管理员与模型配置 → 启动后台 worker。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.router import api_router
from app.core.config import settings
from app.core.utils import new_ulid
from app.db.engine import ensure_database, session_scope
from app.db.migrate import upgrade_to_head
from app.db.seed import ensure_model_configs, sync_model_config_endpoints
from app.services import auth_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("manju")


def bootstrap() -> None:
    """本地开箱即用：没有库就建库，没迁移就迁移，没有种子就种。"""
    created = ensure_database()
    if created:
        logger.info("created database %s", settings.DATABASE_URL)
    upgrade_to_head()
    settings.media_root.mkdir(parents=True, exist_ok=True)
    with session_scope() as db:
        admin = auth_service.ensure_bootstrap_admin(db)
        if admin is not None:
            logger.info("seeded bootstrap admin %r", admin.username)
        created_configs = ensure_model_configs(db)
        if created_configs:
            logger.info("seeded %d model configs", created_configs)
        synced = sync_model_config_endpoints(db)
        if synced:
            logger.info("synced %d model config endpoints", synced)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    bootstrap()
    worker = None
    if settings.WORKER_ENABLED:
        from app.services.worker import worker as background_worker

        worker = background_worker
        worker.start()
    logger.info(
        "%s v%s started, mode=%s", settings.APP_NAME, __version__, settings.APP_MODE
    )
    yield
    if worker is not None:
        worker.stop()
    logger.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.APP_NAME} API",
        version=__version__,
        description=(
            "大纲 → 剧本 → 分镜 → 图片 → 视频 的可追溯生产接口。"
            "完整契约见仓库 docs/openapi.yaml。"
        ),
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = (request.headers.get("X-Request-Id") or "").strip()[:64]
        request.state.request_id = request_id or f"req_{new_ulid()}"
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_PREFIX)

    def custom_openapi() -> dict:
        """让 /docs 与静态契约口径一致：全局 bearer 鉴权 + 公开路由显式 security: []。"""
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque-session-token",
            "description": "登录返回的随机会话 token；服务端只保存它的 SHA-256 哈希。",
        }
        # 全局默认需要鉴权；公开路由用 openapi_extra={"security": []} 覆盖
        schema["security"] = [{"bearerAuth": []}]
        schema["servers"] = [
            {"url": settings.API_PREFIX, "description": "本地开发（后端端口 8010）"}
        ]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "name": settings.APP_NAME,
            "version": __version__,
            "docs": "/docs",
            "api": settings.API_PREFIX,
        }

    return app


app = create_app()
