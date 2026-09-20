"""从「已生成剧本」的状态继续跑真实链路：资产规划 → 出图 → 立绘派生 → 视频方案 → 视频。

真实豆包文本调用很慢（单次 80–190 秒），seed_demo 的一口气流程容易被超时打断；
本脚本按现有项目续跑剩下的步骤，每步单独等待、失败即停并打印供应商错误。

用法：
    run.cmd scripts\\continue_real_demo.py <projectId>
"""

from __future__ import annotations

import os
import sys
import time

import httpx

PORT = os.environ.get("MANJU_BACKEND_PORT", "8010")
BASE = f"http://127.0.0.1:{PORT}/api/v1"
TERMINAL = {"succeeded", "failed", "cancelled"}


class Api:
    def __init__(self, username: str, password: str) -> None:
        self.client = httpx.Client(base_url=BASE, timeout=120.0)
        token = self.client.post(
            "/auth/login", json={"username": username, "password": password}
        ).json()["sessionToken"]
        self.client.headers["Authorization"] = f"Bearer {token}"

    def action(self, path: str, key: str, body: dict | None = None) -> str:
        response = self.client.post(
            path, json=body or {}, headers={"Idempotency-Key": key}
        )
        if response.status_code >= 400:
            raise SystemExit(f"{path} -> {response.status_code} {response.text}")
        return response.json()["submissionId"]

    def wait(self, submission_id: str, timeout: float, label: str) -> dict:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            detail = self.client.get(f"/submissions/{submission_id}").json()
            status = detail["submission"]["logicalStatus"]
            if status in TERMINAL:
                elapsed = time.monotonic() - started
                if status != "succeeded":
                    raise SystemExit(
                        f"[{label}] 失败（{elapsed:.0f}s）："
                        f"{detail['submission']['errorCode']} "
                        f"{detail['submission']['errorMessage']}"
                    )
                usages = [
                    (a["usage"] or {}).get("outputUnits") for a in detail["attempts"]
                ]
                print(f"[{label}] 成功，耗时 {elapsed:.0f}s，输出用量 {usages}")
                return detail
            time.sleep(2.0)
        raise SystemExit(f"[{label}] 等待超时（{timeout:.0f}s）")


def media_node(client: httpx.Client, media_id: str) -> dict:
    return client.get(f"/media-nodes/{media_id}").json()


def canvas_node_id_of(client: httpx.Client, episode_id: str, media_id: str) -> str:
    canvas = client.get(f"/episodes/{episode_id}/canvas").json()
    for item in canvas["nodes"]:
        if item["node"]["mediaId"] == media_id:
            return item["node"]["id"]
    raise SystemExit(f"画布上找不到媒体节点 {media_id}")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：continue_real_demo.py <projectId>")
        return 2
    project_id = sys.argv[1]
    api = Api("admin", "admin123")
    client = api.client
    stamp = int(time.time())

    project = client.get(f"/projects/{project_id}").json()
    episode_id = project["episodes"][0]["id"]
    canvas = client.get(f"/episodes/{episode_id}/canvas").json()

    script_doc = None
    for item in canvas["nodes"]:
        if item["node"]["nodeType"] != "text" or not item["node"]["documentId"]:
            continue
        detail = client.get(f"/canvas/nodes/{item['node']['id']}").json()
        if detail["document"]["document"]["kind"] == "script":
            script_doc = detail["document"]["document"]["id"]
            break
    if not script_doc:
        raise SystemExit("画布上找不到剧本节点")

    clips = client.get(f"/episodes/{episode_id}/clips").json()["items"]
    clip = clips[0]
    print(f"剧本文档 {script_doc}；Clip {len(clips)} 个，用第 1 个：{clip['title']}")

    # 1) 资产规划（真实豆包产清单）。若已有方案节点则复用，避免重复调用模型。
    existing = [
        asset
        for asset in client.get(f"/projects/{project_id}/assets").json()["items"]
        if asset["mediaNodeId"]
    ]
    if existing:
        plan_nodes = [asset["mediaNodeId"] for asset in existing]
        print(f"复用已有 {len(plan_nodes)} 个图片方案节点：{[a['canonicalName'] for a in existing]}")
    else:
        plan_sub = api.action(
            f"/documents/{script_doc}/actions/plan-characters", f"real-plan-{stamp}", {}
        )
        plan = api.wait(plan_sub, 900, "plan-characters")
        items = plan["result"]["assets"]
        print(f"     资产清单：{[i['canonicalName'] for i in items]}")

        confirm_response = client.post(
            "/asset-plans/confirm",
            json={"submissionId": plan["submission"]["id"], "items": items},
        )
        if confirm_response.status_code >= 400:
            raise SystemExit(
                f"确认清单失败：{confirm_response.status_code} {confirm_response.text}"
            )
        confirmed = confirm_response.json()
        if confirmed.get("droppedClipIds"):
            print(f"     （模型给出的 {len(confirmed['droppedClipIds'])} 个 Clip id 不存在，已丢弃）")
        plan_nodes = [node["id"] for node in confirmed["createdMediaNodes"]]
        print(f"     已确认，图片方案节点 {len(plan_nodes)} 个，回填引用 {len(confirmed['references'])} 条")

    # 2) 每个方案节点各出一张（真实 Seedream）——每个角色都要有图，
    #    视频方案的前置校验要求「每项所需资产恰有一个可用图片结果」。
    portraits: list[str] = []
    for index, plan_node in enumerate(plan_nodes):
        detail = media_node(client, plan_node)
        results = [
            candidate
            for candidate in client.get(f"/assets/{detail['mediaNode']['assetId']}").json()[
                "candidates"
            ]
            if candidate["mediaKind"] == "image_result" and not candidate["blocked"]
        ]
        if results:
            portraits.append(results[0]["id"])
            print(f"     复用已有图片结果 {results[0]['id']}")
            continue
        draw = api.wait(
            api.action(
                f"/media/{plan_node}/draw",
                f"real-draw-{stamp}-{index}",
                {"count": 1, "seed": -1},
            ),
            900,
            f"image_draw[{index}]",
        )
        result = client.get(f"/canvas/nodes/{draw['producedNodeIds'][0]}").json()["mediaNode"]
        portraits.append(result["mediaNode"]["id"])
        print(
            f"     出图 {result['media']['width']}x{result['media']['height']} "
            f"decode={result['media']['decodeStatus']}"
        )

    # 3) 立绘联合派生（真实两次生图），只对第一张立绘做，随后屏蔽多余候选
    derive = api.wait(
        api.action(f"/media-nodes/{portraits[0]}/derive", f"real-derive-{stamp}"),
        1200,
        "image_derive",
    )
    derived = [
        client.get(f"/canvas/nodes/{node_id}").json()["mediaNode"]["mediaNode"]["id"]
        for node_id in derive["producedNodeIds"]
    ]
    for media_id in derived:
        client.patch(f"/media-nodes/{media_id}", json={"blocked": True})
    print(f"     派生 {len(derived)} 张，已屏蔽其余候选，保留唯一有效结果")

    for asset in client.get(f"/projects/{project_id}/assets").json()["items"]:
        usable = client.get(f"/assets/{asset['id']}").json()["usableResultCount"]
        print(f"     资产 {asset['canonicalName']} 可用图片结果数 = {usable}")

    # 5) 视频方案：冻结 Clip 与参考图
    plan_response = client.post(
        f"/clips/{clip['id']}/video-plans",
        json={"requirement": "雨夜重逢，克制，无字幕"},
        headers={"Idempotency-Key": f"real-vplan-{stamp}"},
    )
    if plan_response.status_code >= 400:
        raise SystemExit(f"视频方案失败：{plan_response.text}")
    body = plan_response.json()
    api.wait(body["submissionId"], 600, "video_plan_create")
    video_plan = body["mediaNode"]["id"]
    frozen = media_node(client, video_plan)
    print(f"     方案 {video_plan}，冻结参考图 {len(frozen['referenceImages'])} 张")

    # 6) 视频抽卡（Seedance mini，真实）
    video_draw = api.wait(
        api.action(f"/media/{video_plan}/draw", f"real-vdraw-{stamp}", {"count": 1, "seed": -1}),
        1800,
        "video_draw",
    )
    result = client.get(f"/canvas/nodes/{video_draw['producedNodeIds'][0]}").json()["mediaNode"]
    print(
        f"     视频 {result['media']['width']}x{result['media']['height']} "
        f"{result['media']['durationMs']}ms decode={result['media']['decodeStatus']} "
        f"bytes={result['media']['sizeBytes']}"
    )

    # 7) 费用与交付汇总
    summary = client.get(f"/projects/{project_id}/billing/summary").json()
    print()
    print(f"attempt 数 {summary['attemptCount']}；实际支出 {summary['actualTotal']} "
          f"{summary['currency']}；待核对 {summary['unknownUsageCount']} 条")
    canvas = client.get(f"/episodes/{episode_id}/canvas").json()
    print(f"画布节点 {len(canvas['nodes'])} 个，依赖边 {len(canvas['edges'])} 条")
    print(f"前端：http://127.0.0.1:5180/projects/{project_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
