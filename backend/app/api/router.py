"""路由聚合。新增领域时在这里挂载。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routers import (
    admin,
    assets,
    auth,
    billing,
    canvas,
    clips,
    documents,
    files,
    generation,
    media,
    projects,
    system,
)

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(projects.router)
api_router.include_router(canvas.router)
api_router.include_router(documents.router)
api_router.include_router(clips.router)
api_router.include_router(assets.router)
api_router.include_router(media.router)
api_router.include_router(generation.router)
api_router.include_router(billing.router)
api_router.include_router(files.router)

__all__ = ["api_router"]
