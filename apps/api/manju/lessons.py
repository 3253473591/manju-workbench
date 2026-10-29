"""lesson：Agent 从失败里学到的经验（`01` §4.8 / `03` §4.2）。

只写结构化记录，不维护散文文档。天然键 `(scope, failure_type, symptom_key)`：
命中就 `hit_count += 1`，不新建行 —— 这就是"记忆膨胀"的治理方式。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from manju.models import Lesson

ARCHIVE_AFTER = 200
ARCHIVE_IDLE_DAYS = 90


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _symptom_key(symptom: str) -> str:
    """同一句话的细微空白差异不该产生两条记忆。"""
    normalized = " ".join(symptom.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def remember(
    session: Session,
    *,
    scope: str,
    failure_type: str,
    symptom: str,
    fix: str,
    evidence_revision_id: int | None = None,
) -> Lesson:
    key = _symptom_key(symptom)
    lesson = session.scalar(
        select(Lesson).where(
            Lesson.scope == scope,
            Lesson.failure_type == failure_type,
            Lesson.symptom_key == key,
        )
    )
    if lesson is None:
        lesson = Lesson(
            scope=scope,
            failure_type=failure_type,
            symptom=symptom[:500],
            symptom_key=key,
            fix=fix,
            evidence_revision_id=evidence_revision_id,
            hit_count=1,
            last_hit_at=_now(),
            created_at=_now(),
        )
        session.add(lesson)
    else:
        lesson.hit_count += 1
        lesson.last_hit_at = _now()
        lesson.fix = fix
    session.flush()
    return lesson


def recall(session: Session, scope: str, limit: int = 3) -> list[Lesson]:
    rows = session.scalars(
        select(Lesson)
        .where(Lesson.scope == scope, Lesson.archived_at.is_(None))
        .order_by(Lesson.hit_count.desc(), Lesson.last_hit_at.desc())
        .limit(limit)
    )
    return list(rows)
