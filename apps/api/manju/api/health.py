"""健康检查。匿名可访问（`02` §3.8）；DB 不通时返回 degraded，别让探活把进程打死。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from manju.deps import get_session
from manju.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health")
def health(session: Session = Depends(get_session)) -> HealthOut:
    try:
        session.execute(text("SELECT 1"))
        revision = session.scalar(text("SELECT version_num FROM alembic_version"))
        migrated = bool(revision)
    except Exception:  # 探活要回答"活着但不好"，自己不能抛
        return HealthOut(status="degraded", db=False, migrated=False)
    return HealthOut(status="ok", db=True, migrated=migrated)
