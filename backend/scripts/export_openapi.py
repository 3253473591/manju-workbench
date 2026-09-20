"""导出服务端实际生成的 OpenAPI 3.1。

产出：docs/openapi.generated.json
用途：与手写的 docs/openapi.yaml（设计契约）对照；测试 test_openapi_conformance.py
保证两者的 path+method 集合完全一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

OUTPUT = BACKEND_DIR.parent / "docs" / "openapi.generated.json"


def main() -> int:
    from app.main import create_app

    app = create_app()
    spec = app.openapi()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    methods = sum(
        1
        for item in spec["paths"].values()
        for key in item
        if key in {"get", "post", "patch", "delete", "put"}
    )
    print(f"openapi={spec['openapi']} paths={len(spec['paths'])} operations={methods}")
    print(f"written: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
