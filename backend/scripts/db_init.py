"""建库建表脚本（开发/演示用）。

用法：
    run.cmd scripts\\db_init.py            # 用 .env / 环境变量里的 DATABASE_URL
    DATABASE_URL=... run.cmd scripts\\db_init.py

注意：正式建表走 Alembic（backend/alembic），本脚本只用于本地快速自检。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.engine import create_all, ensure_database, get_engine  # noqa: E402


def main() -> int:
    print(f"DATABASE_URL = {settings.DATABASE_URL}")
    created = ensure_database()
    print(f"database created: {created}")
    create_all()
    inspector = inspect(get_engine())
    tables = sorted(inspector.get_table_names())
    print(f"tables ({len(tables)}):")
    for name in tables:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
