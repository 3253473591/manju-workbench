"""把目标库清空（开发/测试用）。

用法：
    set DATABASE_URL=...
    python scripts\\db_reset.py --yes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.engine import drop_all, ensure_database, get_engine  # noqa: E402


def main(argv: list[str]) -> int:
    if "--yes" not in argv:
        print("拒绝执行：需要显式传入 --yes")
        return 2
    print(f"DATABASE_URL = {settings.DATABASE_URL}")
    ensure_database()
    drop_all()
    from sqlalchemy import inspect

    print(f"tables after reset: {sorted(inspect(get_engine()).get_table_names())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
