"""给 e2e / 本地冒烟准备最小数据：一个 lead、一个项目、一集、一个可生成片段。

不依赖素材目录（CI 里没有剧集原片），也不跑任何模型：
只保证前端能登录、分镜表有行、「生成…」能打开付费点弹窗。

用法：
    $env:DATABASE_URL='mysql+pymysql://root:root@127.0.0.1:3306/manju_e2e?charset=utf8mb4'
    python -m alembic upgrade head        # 在 apps/api 下
    python scripts/seed_e2e.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps" / "api"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from manju.db import make_engine  # noqa: E402
from manju.models import Clip, Episode, Project, ProjectMember, User  # noqa: E402
from manju.security import hash_password  # noqa: E402

EMAIL = "seed@example.com"
PASSWORD = "seedpass12345"
TITLE = "E2E 演示项目"
CLIP_TITLE = "烟测片段"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def seed(session: Session) -> None:
    user = session.scalar(select(User).where(User.email == EMAIL))
    if user is None:
        user = User(email=EMAIL, pwd_hash=hash_password(PASSWORD), role="lead", is_active=1, created_at=_now())
        session.add(user)
        session.flush()

    project = session.scalar(select(Project).where(Project.title == TITLE, Project.owner_id == user.id))
    if project is None:
        project = Project(
            title=TITLE, owner_id=user.id, genre="荒诞喜剧", tone="冷静克制",
            logline="一个只背过《弟子规》的野仙，被派去看守一根没人敢碰的铜柱。",
            status="active", version=1, created_at=_now(), updated_at=_now(),
        )
        session.add(project)
        session.flush()
        session.add(ProjectMember(project_id=project.id, user_id=user.id, role="member", created_at=_now()))

    episode = session.scalar(select(Episode).where(Episode.project_id == project.id, Episode.idx == 1))
    if episode is None:
        episode = Episode(
            project_id=project.id, idx=1, title="第一集：烟测", status="draft", version=1,
            created_at=_now(), updated_at=_now(),
        )
        session.add(episode)
        session.flush()

    clip = session.scalar(select(Clip).where(Clip.episode_id == episode.id, Clip.idx == 1))
    if clip is None:
        session.add(
            Clip(
                episode_id=episode.id,
                idx=1,
                source_idx=1,
                title=CLIP_TITLE,
                duration_s=Decimal("6.00"),
                status="prompted",
                prompt="镜头1（0-6秒）：远景急推，人物从雾里走进画面。",
                shots_json=[{"no": 1, "start": 0, "end": 6, "desc": "远景急推"}],
                dialogue_json=[{"speaker": "旁白", "text": "烟测开始", "shot_no": 1, "on_screen": True}],
                acceptance_json=[{"id": "a1", "criterion": "画面有推进，不是定镜"}],
                version=1,
                created_at=_now(),
                updated_at=_now(),
            )
        )
    session.commit()


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("需要 DATABASE_URL（指向已跑过 alembic 的库）")
        return 2
    engine = make_engine(url)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        seed(session)
    engine.dispose()
    print(f"e2e 数据就绪：{EMAIL} / {PASSWORD} → 《{TITLE}》→ {CLIP_TITLE}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
