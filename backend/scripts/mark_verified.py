"""把真实冒烟的结果写回模型配置：核验时间 + 供应商返回的实际模型标识。

只在**确实跑过真实调用**之后使用。seedance-fast / seedance-2.5 本次未发起真实任务，
不写入核验时间——未核验的档位不许显示为可用。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlmodel import select  # noqa: E402

from app.db.engine import session_scope  # noqa: E402
from app.models.model_config import ModelConfig  # noqa: E402

#: product_label -> 供应商响应里的实际模型标识
VERIFIED = {
    "doubao": "doubao-seed-2-1-turbo-260628",
    "seedream-lite": "doubao-seedream-5-0-260128",
    "seedream-pro": "doubao-seedream-5-0-pro-260628",
    "seedance-mini": "seedance-mini（接入点已受理并产出可解码视频）",
}


def main() -> int:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as db:
        for label, actual in VERIFIED.items():
            config = db.exec(
                select(ModelConfig).where(ModelConfig.product_label == label)
            ).first()
            if config is None:
                print(f"跳过（未找到档位）：{label}")
                continue
            config.actual_model = actual
            config.verified_at = now
            db.add(config)
            print(f"已标记核验：{label} -> {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
