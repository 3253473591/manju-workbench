"""用 Google Chrome（CDP）截图。

为什么不用 `chrome --screenshot`：
- 它只在 load 事件后拍一张，SPA 的异步数据还没回来；
- 加 `--virtual-time-budget` 又会被 React Flow 的 requestAnimationFrame 拖住，等不到静止
  （交接文档 §7.6 记过这个坑）。
这里直接用 CDP：设置 localStorage 会话 → 导航 → 等真实时间 → 截图，稳定可重复。

用法：
    python scripts/shot.py --help
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

BACKEND = f"http://127.0.0.1:{__import__('os').environ.get('MANJU_BACKEND_PORT', '8010')}"
FRONTEND = f"http://127.0.0.1:{__import__('os').environ.get('MANJU_WEB_PORT', '5173')}"


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if not found:
        raise SystemExit("找不到 Chrome，请检查安装路径")
    return found


def login(username: str, password: str) -> str:
    payload = json.dumps({"username": username, "password": password}).encode()
    request = urllib.request.Request(
        f"{BACKEND}/api/v1/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["sessionToken"]


class Cdp:
    """极简 CDP 客户端：只用到导航、执行脚本、截图。"""

    def __init__(self, ws_url: str) -> None:
        import websockets.sync.client as ws_client

        self._ws = ws_client.connect(ws_url, max_size=64 * 1024 * 1024)
        self._id = 0

    def send(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params or {}}
        self._ws.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self._ws.recv(timeout=timeout)
            payload = json.loads(raw)
            if payload.get("id") == self._id:
                if "error" in payload:
                    raise RuntimeError(f"{method} 失败：{payload['error']}")
                return payload.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expression: str) -> None:
        self.send("Runtime.evaluate", {"expression": expression, "awaitPromise": True})

    def close(self) -> None:
        self._ws.close()


def wait_devtools(port: int, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page":
                    return target
        except Exception:  # noqa: BLE001 - 端口还没起来
            time.sleep(0.4)
    raise SystemExit("Chrome DevTools 端口未就绪")


def capture(
    *,
    url: str,
    output: Path,
    token: str | None,
    width: int,
    height: int,
    wait: float,
    port: int,
    full_page: bool,
    script: str | None = None,
) -> None:
    chrome = find_chrome()
    profile = Path(__file__).resolve().parents[1] / ".cache" / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(  # noqa: S603
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = wait_devtools(port)
        cdp = Cdp(target["webSocketDebuggerUrl"])
        try:
            cdp.send("Page.enable")
            cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            )
            # 先落到应用同源页面，才能写 localStorage
            cdp.send("Page.navigate", {"url": f"{FRONTEND}/login"})
            time.sleep(1.5)
            if token:
                cdp.evaluate(
                    "localStorage.setItem('manju.token', %s)" % json.dumps(token)
                )
            cdp.send("Page.navigate", {"url": url})
            time.sleep(wait)
            if script:
                cdp.evaluate(script)
                time.sleep(max(wait * 0.6, 2.0))
            params = {"format": "png", "captureBeyondViewport": full_page}
            result = cdp.send("Page.captureScreenshot", params)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(base64.b64decode(result["data"]))
            print(f"截图已保存：{output}")
        finally:
            cdp.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome CDP 截图")
    parser.add_argument("--url", required=True, help="完整 URL 或 / 开头的路径")
    parser.add_argument("--out", required=True, help="输出 PNG 路径")
    parser.add_argument("--as", dest="username", default="", help="登录用户名（留空则匿名）")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=920)
    parser.add_argument("--wait", type=float, default=4.0, help="导航后等待秒数")
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--full-page", action="store_true")
    parser.add_argument(
        "--script",
        default=None,
        help="导航并等待后执行的 JS（例如点开某个节点），执行完再截一张",
    )
    args = parser.parse_args()

    url = args.url if args.url.startswith("http") else f"{FRONTEND}{args.url}"
    token = login(args.username, args.password) if args.username else None
    capture(
        url=url,
        output=Path(args.out),
        token=token,
        width=args.width,
        height=args.height,
        wait=args.wait,
        port=args.port,
        full_page=args.full_page,
        script=args.script,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
