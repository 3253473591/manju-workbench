"""把仓库里现成的规则文档灌成 Skill（`01` §4.9 / PRD §7.1）。

    python scripts/seed_skills.py            # 幂等：body 不变就不产生新版本

五份都来自真实文档，不是新写的占位内容：
    seedance-final-prompt  ← 提示词/Seedance2.0/Seedance 2.0 系列提示词指南.md   （S4）
    story-to-script        ← 我师父是个普通人/第一集/剧本.md 的 §1+§2             （S3）
    krea2-character-card   ← 我师父是个普通人/人物卡/Krea2人物卡提示词.md        （S5）
    concept-sheet          ← 我师父是个普通人/人物卡/概念设定案提示词.md          （S5）
    video-motion-review    ← video-motion-review/SKILL.md                       （review）

切节、token 估算、版本切换全部走 `manju.skills`，与本服务运行期读的是同一份实现。
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "apps" / "api" if (ROOT / "apps" / "api" / "manju").is_dir() else ROOT
DOCS = ROOT / "docs"

sys.path.insert(0, str(API_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from manju import skills  # noqa: E402
from manju.db import make_engine  # noqa: E402
from manju.models import User  # noqa: E402
from manju.security import hash_password  # noqa: E402

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin12345"

# (name, stage, 源文件, 说明, tool_ref)
SOURCES = (
    ("seedance-final-prompt", "S4", "提示词/Seedance2.0/Seedance 2.0 系列提示词指南.md", "分镜时序与运镜写法", None),
    ("story-to-script", "S3", "我师父是个普通人/第一集/剧本.md", "分集剧本结构：制作规范 + 角色消歧", None),
    ("krea2-character-card", "S5", "我师父是个普通人/人物卡/Krea2人物卡提示词.md", "Krea2 人物卡", None),
    ("concept-sheet", "S5", "我师父是个普通人/人物卡/概念设定案提示词.md", "三视图 / 面部特写", None),
    ("video-motion-review", "review", "video-motion-review/SKILL.md", "动线扫描与取帧审片", "vscan"),
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _script_rules(body: str) -> str:
    """剧本里只有 §1（制作规范）和 §2（角色与资产规划）是规则，§3 之后是这一集的内容。"""
    start = body.find("## 1.")
    end = body.find("## 3.")
    if start < 0 or end <= start:
        raise SystemExit("剧本.md 结构变了：找不到 §1..§3")
    return body[start:end].strip()


def _admin(session: Session) -> User:
    user = session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    if user is None:
        user = User(
            email=ADMIN_EMAIL,
            pwd_hash=hash_password(ADMIN_PASSWORD),
            role="admin",
            is_active=1,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(user)
        session.flush()
    return user


def main() -> int:
    with Session(make_engine()) as session:
        admin = _admin(session)
        total_sections = 0
        for name, stage, relative, note, tool_ref in SOURCES:
            path = DOCS / relative
            if not path.exists():
                print(f"[skip] 缺少源文件：{relative}")
                continue
            body = _read(path)
            if name == "story-to-script":
                body = _script_rules(body)
            version, created = skills.upsert_skill(
                session,
                name=name,
                stage=stage,
                body_md=body,
                created_by=admin.id,
                note=note,
                tool_ref=tool_ref,
            )
            sections = version.sections_json or []
            total_sections += len(sections)
            tokens = sum(section["tokens_est"] for section in sections)
            action = "新建" if created else "复用"
            print(f"[{action}] {name:22} {stage:6} v{version.version} 节={len(sections):3} tokens≈{tokens}")
        session.commit()
        print(f"合计 {total_sections} 节")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
