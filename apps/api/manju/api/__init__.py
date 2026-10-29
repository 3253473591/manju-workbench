"""`/api/v1` 下的路由聚合。

没有出现的路由就是**不存在**（`03` §12.2）：缺失的服务层宁可 404 也不留占位实现 ——
占位会让人误判范围。当前唯一还空着的是 `/admin/providers`（等 provider 路由表配置）。
"""

from __future__ import annotations

from fastapi import APIRouter

from manju.api import (
    admin,
    assets,
    auth,
    clips,
    collab,
    episodes,
    events,
    health,
    images,
    jobs_api,
    projects,
    skills_api,
    steps_api,
)

api = APIRouter(prefix="/api/v1")
api.include_router(health.router)
api.include_router(auth.router)
api.include_router(projects.router)
api.include_router(episodes.router)
api.include_router(clips.router)
api.include_router(assets.router)
api.include_router(jobs_api.router)
api.include_router(steps_api.router)
api.include_router(skills_api.router)
api.include_router(collab.router)
api.include_router(events.router)
api.include_router(admin.router)
api.include_router(images.router)
