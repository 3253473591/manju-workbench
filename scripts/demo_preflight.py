"""开录前自检：把"演示脚本依赖的事实"全部机器验一遍。

为什么要脚本而不是清单：演示翻车基本都来自**状态漂移**（某个片段不再在 review、资产被删、
provider 被上一轮演练停用了），这些都能查出来，没人愿意开录前一条条点。

用法：
    $env:DATABASE_URL='mysql+pymysql://root:root@127.0.0.1:3306/manju_dev?charset=utf8mb4'
    python scripts/demo_preflight.py --api http://127.0.0.1:8000

不带 `--api` 只查库（离线也能用）。退出码 1 = 有阻断项，0 = 可以开录。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pymysql

OWNER = ("seed@example.com", "seedpass12345")
ADMIN = ("admin@example.com", "admin12345")
PROJECT_ID = 1


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


def _db(url: str) -> pymysql.connections.Connection:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
        database=(parsed.path or "/").lstrip("/"),
    )


def _migration_head(api_root: Path) -> str:
    """从迁移文件算出 head：没被任何 down_revision 指向的那个 revision。"""
    import re

    revisions: dict[str, str | None] = {}
    for path in (api_root / "migrations" / "versions").glob("*.py"):
        body = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision = "([^"]+)"', body, re.M)
        down = re.search(r'^down_revision = (?:None|"([^"]+)")', body, re.M)
        if revision:
            revisions[revision.group(1)] = down.group(1) if down else None
    children = {parent for parent in revisions.values() if parent}
    heads = [revision for revision in revisions if revision not in children]
    return max(heads) if heads else "?"


def check_database(url: str) -> list[Check]:
    checks: list[Check] = []
    try:
        conn = _db(url)
    except Exception as exc:  # noqa: BLE001 - 自检脚本要把"连不上"说清楚，而不是抛栈
        return [Check("数据库可连接", False, f"{type(exc).__name__}: {exc}")]

    with conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        version = (cur.fetchone() or ["?"])[0]
        expected = _migration_head(Path(__file__).resolve().parent.parent / "apps" / "api")
        checks.append(Check("迁移到 head", version == expected, f"alembic={version}（期望 {expected}）"))

        cur.execute("SELECT title, status FROM project WHERE id = %s", (PROJECT_ID,))
        row = cur.fetchone()
        checks.append(Check("演示项目存在且 active", bool(row) and row[1] == "active", f"{row}"))
        if not row:
            conn.close()
            return checks

        cur.execute("SELECT idx, COUNT(*) FROM episode WHERE project_id = %s GROUP BY idx ORDER BY idx", (PROJECT_ID,))
        episodes = cur.fetchall()
        checks.append(
            Check("第一集 + 第二集", [e[0] for e in episodes] == [1, 2], f"集={[e[0] for e in episodes]}")
        )

        cur.execute(
            """SELECT e.idx, cl.status, COUNT(*) FROM clip cl JOIN episode e ON e.id = cl.episode_id
               WHERE e.project_id = %s GROUP BY e.idx, cl.status""",
            (PROJECT_ID,),
        )
        by_state: dict[tuple[int, str], int] = {(row[0], row[1]): row[2] for row in cur.fetchall()}
        review = sum(count for (idx, state), count in by_state.items() if state == "review")
        approved = sum(count for (idx, state), count in by_state.items() if state == "approved")
        checks.append(Check("有 review 片段可演「打回」", review >= 1, f"review={review} approved={approved}"))

        cur.execute(
            """SELECT COUNT(*) FROM revision r JOIN clip cl ON cl.id = r.target_id
               JOIN episode e ON e.id = cl.episode_id
               WHERE e.project_id = %s AND r.output_asset_id IS NOT NULL""",
            (PROJECT_ID,),
        )
        takes = cur.fetchone()[0]
        checks.append(Check("有可回滚的 take", takes >= 3, f"带产物的 revision={takes}"))

        cur.execute(
            """SELECT a.id, COUNT(DISTINCT e.id) AS eps FROM asset a
               JOIN clip cl ON JSON_CONTAINS(cl.ref_slots_json, JSON_OBJECT('asset_id', a.id))
               JOIN episode e ON e.id = cl.episode_id
               WHERE e.project_id = %s GROUP BY a.id HAVING eps > 1 LIMIT 1""",
            (PROJECT_ID,),
        )
        shared = cur.fetchone()
        checks.append(Check("有跨集复用的资产可演", shared is not None, f"asset={shared}"))

        cur.execute(
            """SELECT COUNT(*) FROM skill_version sv JOIN skill s ON s.id = sv.skill_id
               WHERE s.status = 'active' AND sv.superseded_at IS NULL"""
        )
        skills = cur.fetchone()[0]
        checks.append(Check("Skill 已灌入且各有生效版本", skills >= 3, f"生效版本={skills}"))

        cur.execute("SELECT COUNT(*) FROM runtime_flag WHERE enabled = 0")
        disabled = cur.fetchone()[0]
        checks.append(
            Check("没有通道处于停用状态", disabled == 0, f"停用中={disabled}（备用 A 演完记得启用）")
        )
    conn.close()

    media_root = os.environ.get("MEDIA_ROOT", "")
    if media_root:
        root = Path(media_root)
        checks.append(Check("MEDIA_ROOT 可读", root.is_dir() and any(root.iterdir()), str(root)))
    else:
        checks.append(Check("MEDIA_ROOT 已设置", False, "未设置：取图会 404", blocking=True))
    return checks


def check_api(api: str) -> list[Check]:
    checks: list[Check] = []
    base = api.rstrip("/")
    try:
        with httpx.Client(timeout=10) as client:
            health = client.get(f"{base}/api/v1/health")
            checks.append(Check("API 可访问", health.status_code == 200, f"{health.status_code}"))

            login = client.post(f"{base}/api/v1/auth/login", json={"email": OWNER[0], "password": OWNER[1]})
            checks.append(Check("演示账号可登录", login.status_code == 200, str(login.status_code)))
            if login.status_code != 200:
                return checks

            projects = client.get(f"{base}/api/v1/projects").json().get("items", [])
            active = [p for p in projects if p["status"] == "active"]
            checks.append(Check("有 active 项目", len(active) >= 1, f"active={len(active)}"))

            clips = client.get(f"{base}/api/v1/projects/{PROJECT_ID}/clips").json()["items"]
            with_prompt = [c for c in clips if c.get("prompt")]
            checks.append(
                Check("「生成…」可点（有带提示词的片段）", len(with_prompt) >= 1, f"带提示词={len(with_prompt)}/{len(clips)}")
            )

            payload = client.get(f"{base}/api/v1/skills").json()
            rows = payload if isinstance(payload, list) else payload.get("items", [])
            checks.append(Check("Skill 维护页有数据", len(rows) >= 3, f"skills={len(rows)}"))
        with httpx.Client(timeout=10) as client:
            client.post(f"{base}/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
            providers = client.get(f"{base}/api/v1/admin/providers").json()
            modes = {name: providers[name]["real"] for name in ("text", "vision", "video", "image")}
            checks.append(
                Check(
                    "provider 模式符合录屏口径（默认全 mock）",
                    not any(modes.values()),
                    f"real={modes}（要真跑请改用真 key 并重录实测段）",
                    blocking=False,
                )
            )
            disabled = [name for name in ("text", "vision", "video", "image") if not providers[name]["enabled"]]
            checks.append(Check("四通道全部启用", not disabled, f"停用={disabled}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("API 检查", False, f"{type(exc).__name__}: {exc}"))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="", help="例如 http://127.0.0.1:8000；不传则只查库")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL", "mysql+pymysql://root:root@127.0.0.1:3306/manju_dev?charset=utf8mb4")
    checks = check_database(url)
    if args.api:
        checks += check_api(args.api)

    width = max(len(item.name) for item in checks) + 2
    for item in checks:
        mark = "OK  " if item.ok else ("FAIL" if item.blocking else "WARN")
        print(f"[{mark}] {item.name.ljust(width)} {item.detail}")
    blockers = [item for item in checks if not item.ok and item.blocking]
    print()
    if blockers:
        print(f"❌ {len(blockers)} 项阻断，先修再开录：")
        for item in blockers:
            print(f"   - {item.name}: {item.detail}")
        return 1
    print("✅ 可以开录")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
