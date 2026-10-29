"""演示录屏驱动：CDP 驱动前台 Chrome + OBS 录制 + 生成字幕。

用法：
    python scripts/record_demo.py            # 干跑：只点、不录，每拍存一张截图到 docs/demo/dry
    python scripts/record_demo.py --record   # 正式录：OBS 开始/结束，输出 mkv + srt

前置：后端 8010、前端 5177、Chrome 9222（带 --remote-debugging-port）、OBS 4455（websocket）。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

import httpx
import websocket

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "docs" / "demo"
PROJECT_WORK = "师尊别砍我系统"
API = "http://127.0.0.1:8010/api/v1"
CLIP_REVIEW = "师尊暴怒，万鬼锁定！"   # review 态：演打回/重发/回滚（开录前由 ensure_state 校正）
CLIP_GEN = "虚空粉碎！滑板师尊登场"    # 演付费点生成

captions: list[tuple[float, str]] = []
started_at = 0.0


def stamp() -> float:
    return time.time() - started_at


class Ui:
    def __init__(self) -> None:
        targets = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list"))
        page = next(t for t in targets if t["type"] == "page" and "5177" in t["url"])
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
        self.rid = 0
        self.cmd("Runtime.enable")
        self.cmd("Page.enable")

    def cmd(self, method: str, params: dict | None = None) -> dict:
        self.rid += 1
        self.ws.send(json.dumps({"id": self.rid, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.rid:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def js(self, expression: str):
        result = self.cmd(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}
        ).get("result", {})
        if result.get("subtype") == "error":
            raise RuntimeError(result.get("description"))
        return result.get("value")

    def wait(self, expression: str, timeout: float = 20.0, label: str = "") -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js(expression):
                return True
            time.sleep(0.15)
        raise RuntimeError(f"等待超时：{label or expression}")

    def click(self, text: str, nth: int = 0) -> None:
        done = self.js(
            f"""(() => {{
              const list = [...document.querySelectorAll('button')]
                .filter(b => b.textContent.trim() === {json.dumps(text, ensure_ascii=False)});
              if (!list[{nth}]) return false;
              list[{nth}].click(); return true;
            }})()"""
        )
        if not done:
            raise RuntimeError(f"按钮不存在：{text}[{nth}]")

    def click_in_row(self, row_text: str, button_text: str, tag: str = "tr") -> None:
        done = self.js(
            f"""(() => {{
              const rows = [...document.querySelectorAll({json.dumps(tag)})]
                .filter(r => r.innerText.includes({json.dumps(row_text, ensure_ascii=False)}));
              for (const row of rows) {{
                const btn = [...row.querySelectorAll('button')]
                  .find(b => b.textContent.trim() === {json.dumps(button_text, ensure_ascii=False)});
                if (btn) {{ btn.click(); return true; }}
              }}
              return false;
            }})()"""
        )
        if not done:
            raise RuntimeError(f"行内按钮不存在：{row_text} → {button_text}")

    def fill(self, selector: str, value: str) -> None:
        self.js(
            f"""(() => {{
              const el = document.querySelector({json.dumps(selector)});
              if (!el) return false;
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
              setter.call(el, {json.dumps(value)});
              el.dispatchEvent(new Event('input', {{bubbles: true}}));
              el.dispatchEvent(new Event('change', {{bubbles: true}}));
              return true;
            }})()"""
        )

    def select(self, index: int, value: str) -> None:
        self.js(
            f"""(() => {{
              const s = document.querySelectorAll('select')[{index}];
              s.value = {json.dumps(value)};
              s.dispatchEvent(new Event('change', {{bubbles: true}}));
            }})()"""
        )

    def type_into(self, selector: str, value: str) -> None:
        """用 CDP 真输入打字（不是 JS 赋值）：受控组件 + Chrome 自动填充下，只有真键盘事件才稳。"""
        self.js(f"(() => {{ const el = document.querySelector({json.dumps(selector)}); el.focus(); el.select(); }})()")
        self.cmd("Input.insertText", {"text": value})

    def login_as(self, email: str, password: str) -> None:
        """填表单并**回读校验**：自动填充/受控组件都会让值回弹，校验不过就重填。"""
        self.wait("!!document.querySelector('input[type=password]')", timeout=15, label="登录表单")
        values: list[str] = []
        for _ in range(4):
            self.type_into("input:not([type=password]):not([type=hidden])", email)
            self.type_into("input[type=password]", password)
            time.sleep(0.3)
            values = self.js("[...document.querySelectorAll('input')].map(i => i.value)")
            if values[:2] == [email, password]:
                break
        else:
            raise RuntimeError(f"登录表单填不进去：{values[:2]}")
        self.click("登录")

    def text(self) -> str:
        return self.js("document.body.innerText") or ""

    def shot(self, path: Path) -> None:
        data = self.cmd("Page.captureScreenshot", {"format": "png"})["data"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))


class Obs:
    def __init__(self) -> None:
        self.ws = websocket.create_connection("ws://127.0.0.1:4455", timeout=15)
        json.loads(self.ws.recv())
        self.ws.send(json.dumps({"op": 1, "d": {"rpcVersion": 1}}))
        json.loads(self.ws.recv())
        self.rid = 0

    def req(self, kind: str) -> dict:
        self.rid += 1
        self.ws.send(json.dumps({"op": 6, "d": {"requestType": kind, "requestId": str(self.rid), "requestData": {}}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("op") == 7 and message["d"].get("requestId") == str(self.rid):
                return message["d"]


def _wait_clip(client: httpx.Client, clip_id: int, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        clip = client.get(f"{API}/clips/{clip_id}").json()
        if clip["status"] in ("review", "approved", "failed"):
            return clip
        time.sleep(0.6)
    raise RuntimeError(f"片段 {clip_id} 没跑完")


def _to_review(client: httpx.Client, clip: dict) -> dict:
    """把片段弄到 review 态：approved 直接重开审批；rejected 用一次重发跑回来。"""
    if clip["status"] == "approved":
        reopened = client.patch(
            f"{API}/clips/{clip['id']}", json={"status": "review"},
            headers={"If-Match": f'"{clip["version"]}"'},
        )
        assert reopened.status_code == 200, reopened.text
        return reopened.json()
    if clip["status"] == "rejected":
        submitted = client.post(
            f"{API}/clips/{clip['id']}/reroll?mode=default",
            json={"res": "draft", "budget_limit": "12.000000"},
            headers={"Idempotency-Key": f"prep-{clip['id']}-{int(time.time())}", "If-Match": f'"{clip["version"]}"'},
        )
        assert submitted.status_code == 202, submitted.text
        client.post(f"{API}/jobs/{submitted.json()['job']['id']}/confirm")
        clip = _wait_clip(client, clip["id"])
        if clip["status"] == "approved":
            reopened = client.patch(
                f"{API}/clips/{clip['id']}", json={"status": "review"},
                headers={"If-Match": f'"{clip["version"]}"'},
            )
            assert reopened.status_code == 200, reopened.text
            return reopened.json()
        return clip
    return clip


def ensure_state() -> tuple[str, str, int]:
    """把演示状态摆好：一个 review 片段（演打回）、一个可生成片段、一张被引用的资产。"""
    with httpx.Client(timeout=60) as client:
        client.post(f"{API}/auth/login", json={"email": "seed@example.com", "password": "seedpass12345"})
        episode = client.get(f"{API}/projects/1/episodes").json()[0]
        clips = client.get(f"{API}/episodes/{episode['id']}/clips").json()
        pool = [c for c in clips if c.get("prompt")]

        review = [c for c in pool if c["status"] == "review"]
        target = review[0] if review else None
        if target is None:
            target = _to_review(client, pool[0])
        generate = next(
            (c for c in pool if c["id"] != target["id"] and c["status"] in ("approved", "review")),
            None,
        )
        if generate is None:
            generate = _to_review(client, next(c for c in pool if c["id"] != target["id"]))

        assets = client.get(f"{API}/assets?project_id=1&limit=200").json()["items"]
        referenced = fallback = None
        for asset in assets:
            probe = client.delete(f"{API}/assets/{asset['id']}")
            if probe.status_code != 409:
                continue
            usages = probe.json()["error"]["detail"]["usages"]
            fallback = fallback or asset["id"]
            if any("slot_idx" in usage for usage in usages):
                referenced = asset["id"]
                break
        referenced = referenced or fallback
        assert referenced is not None, "没有找到被引用的资产"
        print(f"演示状态：打回={target['idx']}（{target['status']}→review）；生成={generate['idx']}（{generate['status']}）；资产=#{referenced}")
        return target["title"], generate["title"], referenced


def beat(ui: Ui, text: str, hold: float, shot_dir: Path | None, name: str) -> None:
    captions.append((stamp(), text))
    print(f"[{stamp():6.1f}s] {name}: {text}")
    time.sleep(hold)
    if shot_dir is not None:
        ui.shot(shot_dir / f"{len(captions):02d}-{name}.png")


def run(record: bool) -> int:
    global started_at
    DEMO.mkdir(parents=True, exist_ok=True)
    dry_dir = DEMO / "dry" if not record else None
    global CLIP_REVIEW, CLIP_GEN
    CLIP_REVIEW, CLIP_GEN, asset_id = ensure_state()
    ui = Ui()
    obs = Obs() if record else None

    # 刚用 API 改过片段状态（ensure_state），前端还拿着旧缓存 —— 强制刷新一次
    ui.js("window.location.reload()")
    time.sleep(0.6)
    ui.wait("document.body.innerText.length > 20", timeout=20, label="页面加载")
    time.sleep(1.8)

    # 开录前归一化：任意残留状态（弹窗 / admin 会话 / 别的项目）都拉回"lead + 第一项目第一集 + 流水线"
    if ui.js("document.body.innerText.includes('付费点')"):
        ui.js("[...document.querySelectorAll('button')].find(b => b.textContent.trim() === '关闭')?.click()")
        time.sleep(0.5)
    if ui.js("document.body.innerText.includes('Skill 维护')") or not ui.js("document.body.innerText.includes('＋项目')"):
        ui.js("[...document.querySelectorAll('button')].find(b => b.textContent.trim() === '退出')?.click()")
        ui.wait("!!document.querySelector('input[type=password]')", timeout=15, label="登录页")
        time.sleep(0.5)
        ui.login_as("seed@example.com", "seedpass12345")
        ui.wait("document.body.innerText.includes('流水线')", timeout=20, label="lead 登录")
    ui.js("window.scrollTo(0, 0)")
    ui.click("流水线")
    ui.select(0, "1")
    time.sleep(1.2)
    ui.select(1, ui.js("document.querySelectorAll('select')[1].value"))
    time.sleep(0.8)

    if record:
        started_at = time.time()
        print("OBS:", obs.req("StartRecord")["requestStatus"])
        time.sleep(1.2)  # 首帧留白，避免字幕压在第一帧
    else:
        started_at = time.time()

    try:
        # ① 流水线
        ui.click("流水线")
        beat(ui, "① 受控的 AI 漫剧生产链路 —— S1–S5 共用一条代码路径", 3.0, dry_dir, "pipeline")

        # ② 分镜表
        ui.click("分镜表")
        ui.wait("document.body.innerText.includes('槽位')")
        time.sleep(0.6)
        beat(ui, "② 一集 8 个片段：状态 / 镜头 / 槽位 / 成本都在一张表里", 4.0, dry_dir, "table")

        # ③ 镜头墙
        ui.click("镜头墙")
        ui.wait("document.querySelectorAll('img').length >= 3", timeout=25)
        time.sleep(1.2)
        beat(ui, "③ 镜头墙：真实成片缩略图，跨集可见", 4.5, dry_dir, "wall")

        # ④ 打回（review 态片段）
        ui.click("分镜表")
        ui.wait("document.body.innerText.includes('槽位')")
        ui.click_in_row(CLIP_REVIEW, "打回")
        time.sleep(1.2)
        beat(ui, "④ 打回：审批绑定在具体 take 上，状态进入 rejected", 3.0, dry_dir, "reject")

        # ⑤ 重发 + 版本链回滚
        ui.click_in_row(CLIP_REVIEW, "重发…")
        ui.wait("document.body.innerText.includes('付费点')", label="重发弹窗")
        time.sleep(0.6)
        has_rollback = ui.js("[...document.querySelectorAll('button')].some(b => b.textContent.trim() === '回滚到此版')")
        beat(ui, "⑤ 重发：历史 take 都在，可回滚；回滚不写新 revision、不花钱", 2.5, dry_dir, "reroll")
        if has_rollback:
            ui.click("回滚到此版")
            time.sleep(1.4)
            beat(ui, "⑤ 回滚到旧版本：片段回到 review，无需重新生成", 2.5, dry_dir, "rollback")
        ui.js("[...document.querySelectorAll('button')].find(b => b.textContent.trim() === '关闭')?.click()")

        # ⑥ 付费点：生成 → 提交预览 → 确认付费
        ui.click_in_row(CLIP_GEN, "生成…")
        ui.wait("document.body.innerText.includes('付费点')", label="付费点弹窗")
        beat(ui, "⑥ 付费点第一步：只填预算上限，提交前不产生任何计费", 3.0, dry_dir, "paypoint-1")
        ui.click("提交（不产生计费）")
        ui.wait("document.body.innerText.includes('预估')", label="预估")
        time.sleep(0.8)
        beat(ui, "⑥ 第二步先看预估金额与 token，再「确认付费」", 3.5, dry_dir, "paypoint-2")
        ui.click("确认付费")
        ui.wait("document.body.innerText.includes('执行时间线')", timeout=40, label="时间线")
        ui.wait("document.body.innerText.includes('done')", timeout=60, label="出片完成")
        time.sleep(1.0)
        beat(ui, "⑥ 出片后：时间线 claim→provider→asset→review→decision，每一步可追", 4.0, dry_dir, "paypoint-3")
        ui.js("[...document.querySelectorAll('button')].find(b => b.textContent.trim() === '关闭')?.click()")

        # ⑦ 资产库：删除被拦
        ui.click("资产库")
        ui.wait("document.querySelectorAll('.mj-card').length >= 3", timeout=25, label="资产卡片")
        time.sleep(0.8)
        picked = ui.js(f"""(() => {{
          const card = [...document.querySelectorAll('.mj-card')].find(c => c.innerText.includes('#{asset_id}'));
          if (!card) return 'missing';
          card.scrollIntoView({{block: 'center'}});
          const btn = [...card.querySelectorAll('button')].find(b => b.textContent.trim() === '删除');
          if (!btn) return 'no-delete';
          btn.click(); return 'clicked';
        }})()""")
        if picked != "clicked":
            raise RuntimeError(f"资产 #{asset_id} 卡片点不到：{picked}")
        ui.wait("document.body.innerText.includes('正被引用')", timeout=20, label="409 引用面板")
        time.sleep(1.0)
        beat(ui, "⑦ 被引用即禁删：409 面板列出引用它的集 / 片段 / 槽位", 4.0, dry_dir, "assets")

        # ⑧ 成本台账
        ui.click("成本台账")
        ui.wait("document.body.innerText.includes('按集')", label="成本页")
        ui.click("按集")
        time.sleep(1.0)
        beat(ui, "⑧ 成本四口径：按模型 / 按集 / 按片段 / 按天", 3.5, dry_dir, "cost")

        # ⑨ 跨集复用：第二集槽位挑第一集的图
        ui.select(1, "4")
        time.sleep(1.5)
        ui.click("分镜表")
        ui.wait("document.body.innerText.includes('槽位')")
        ui.click("槽位")
        time.sleep(1.2)
        beat(ui, "⑨ 资产是项目级的：第二集的槽位直接挑第一集用过的图", 4.0, dry_dir, "cross-episode")
        ui.js("[...document.querySelectorAll('button')].find(b => b.textContent.trim() === '关闭')?.click()")

        # ⑩ 管理面（admin）
        ui.click("退出")
        ui.wait("!!document.querySelector('input[type=password]')", timeout=15, label="登录页")
        time.sleep(0.6)
        ui.login_as("admin@example.com", "admin12345")
        ui.wait("document.body.innerText.includes('Skill 维护')", timeout=20, label="admin 登录")
        ui.click("Skill 维护")
        ui.wait("document.body.innerText.includes('provider')", timeout=20, label="provider 面板")
        time.sleep(1.0)
        beat(ui, "⑩ 管理面：Skill 发布必须试跑；provider 四通道可一键停用（止血开关）", 4.5, dry_dir, "admin")
    finally:
        if record:
            stop = obs.req("StopRecord")
            print("OBS:", stop["requestStatus"])
            time.sleep(2.0)
        write_srt()
    return 0


def write_srt() -> None:
    path = DEMO / "demo.srt"
    lines = []
    for index, (moment, text) in enumerate(captions):
        end = captions[index + 1][0] if index + 1 < len(captions) else moment + 6
        lines.append(f"{index + 1}\n{_ts(moment)} --> {_ts(end)}\n{text}\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    print("字幕:", path)


def _ts(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}".replace(".", ",")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    return run(args.record)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
