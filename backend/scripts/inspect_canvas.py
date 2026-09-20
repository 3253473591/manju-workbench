"""打印某集画布的布局包围盒，用于排查画布缩放问题。"""

from __future__ import annotations

import os
import sys

import httpx

PORT = os.environ.get("MANJU_BACKEND_PORT", "8010")
BASE = f"http://127.0.0.1:{PORT}/api/v1"


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：inspect_canvas.py <episodeId>")
        return 2
    episode_id = sys.argv[1]
    client = httpx.Client(base_url=BASE, timeout=30)
    token = client.post(
        "/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["sessionToken"]
    client.headers["Authorization"] = f"Bearer {token}"

    canvas = client.get(f"/episodes/{episode_id}/canvas").json()
    xs: list[float] = []
    ys: list[float] = []
    for item in canvas["nodes"]:
        layout = item["layout"]
        node = item["node"]
        xs += [layout["x"], layout["x"] + layout["width"]]
        ys += [layout["y"], layout["y"] + layout["height"]]
        print(
            f"{node['nodeType']:6} x={layout['x']:8.1f} y={layout['y']:8.1f} "
            f"w={layout['width']:7.1f} h={layout['height']:7.1f}  {node['label'][:20]}"
        )
    print()
    print(
        f"bbox x[{min(xs):.0f},{max(xs):.0f}] y[{min(ys):.0f},{max(ys):.0f}] "
        f"size {max(xs) - min(xs):.0f} x {max(ys) - min(ys):.0f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
