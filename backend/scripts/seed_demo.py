"""用 HTTP 跑通一遍完整链路，给前端准备可截图的演示数据。

这是 PRD §9 验收 1–7 的接口级复现，也顺便当作前后端联调脚本。

前置：后端已在 127.0.0.1:8010 运行且 WORKER_ENABLED=true。
用法：run.cmd scripts\\seed_demo.py --user admin --password admin123
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

PORT = os.environ.get("MANJU_BACKEND_PORT", "8010")
BASE = f"http://127.0.0.1:{PORT}/api/v1"
OUTLINE = (
    "两个校花女友\n\n"
    "林晚在雨夜校门口等了三十分钟，伞面上的雨声越来越密。\n"
    "她终于看见那个熟悉的身影从雨里走出来，却没有开口。\n"
)
TERMINAL = {"succeeded", "failed", "cancelled"}


class Api:
    def __init__(self, username: str, password: str) -> None:
        self.client = httpx.Client(base_url=BASE, timeout=60.0)
        response = self.client.post(
            "/auth/login", json={"username": username, "password": password}
        )
        response.raise_for_status()
        token = response.json()["sessionToken"]
        self.client.headers["Authorization"] = f"Bearer {token}"

    def action(self, path: str, key: str, body: dict | None = None) -> str:
        response = self.client.post(
            path, json=body or {}, headers={"Idempotency-Key": key}
        )
        if response.status_code >= 400:
            raise SystemExit(f"{path} -> {response.status_code} {response.text}")
        return response.json()["submissionId"]

    def wait(self, submission_id: str, timeout: float = 180.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            detail = self.client.get(f"/submissions/{submission_id}").json()
            status = detail["submission"]["logicalStatus"]
            if status in TERMINAL:
                if status != "succeeded":
                    raise SystemExit(
                        f"提交失败：{detail['submission']['errorCode']} "
                        f"{detail['submission']['errorMessage']}"
                    )
                return detail
            time.sleep(1.0)
        raise SystemExit("等待提交超时")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--name", default="死党落水后，我竟拥有两个校花女友")
    args = parser.parse_args()

    api = Api(args.user, args.password)
    stamp = int(time.time())

    project = api.client.post(
        "/projects", json={"name": args.name, "description": "雨夜重逢 · demo"}
    ).json()
    episode_id = project["episodes"][0]["id"]
    outline_node = project["outlineNodeId"]
    print(f"[1] 项目已建：{project['project']['id']} 第 1 集 {episode_id}")

    node_detail = api.client.get(f"/canvas/nodes/{outline_node}").json()
    outline_doc = node_detail["document"]["document"]["id"]
    api.client.patch(
        f"/documents/{outline_doc}/draft", json={"body": OUTLINE, "title": "两个校花女友"}
    )
    saved = api.client.post(
        f"/documents/{outline_doc}/revisions",
        json={"body": OUTLINE, "title": "两个校花女友"},
    )
    print(f"[2] 大纲已保存 v{saved.json()['currentRevision']['revisionNo']}")

    setting = api.wait(
        api.action(
            f"/documents/{outline_doc}/actions/generate-setting",
            f"demo-setting-{stamp}",
            {"requirement": "按大纲产出主要人物"},
        )
    )
    print(f"[3] 人物设定已生成：{setting['result']['settingDocumentId']}")

    script = api.wait(
        api.action(
            f"/documents/{outline_doc}/actions/generate-script",
            f"demo-script-{stamp}",
            {"requirement": "写第 1 集"},
        )
    )
    script_node = script["producedNodeIds"][0]
    script_doc = api.client.get(f"/canvas/nodes/{script_node}").json()["document"]["document"]["id"]
    print(f"[4] 剧本已生成：{script_doc}")

    split = api.wait(
        api.action(
            f"/documents/{script_doc}/actions/split-clips", f"demo-split-{stamp}", {}
        )
    )
    print(f"[5] 已拆出 {len(split['producedNodeIds'])} 个 Clip 节点")

    clips = api.client.get(f"/episodes/{episode_id}/clips").json()["items"]
    clip = clips[0]

    plan = api.wait(
        api.action(
            f"/documents/{script_doc}/actions/plan-characters",
            f"demo-plan-{stamp}",
            {},
        )
    )
    items = plan["result"]["assets"]
    confirmed = api.client.post(
        "/asset-plans/confirm", json={"submissionId": plan["submission"]["id"], "items": items}
    ).json()
    plan_node = confirmed["createdMediaNodes"][0]["id"]
    print(f"[6] 资产清单已确认：新建方案 {len(confirmed['createdMediaNodes'])}，复用 {len(confirmed['reused'])}")

    draw = api.wait(
        api.action(f"/media/{plan_node}/draw", f"demo-draw-{stamp}", {"count": 1, "seed": -1})
    )
    result_node = api.client.get(
        f"/canvas/nodes/{draw['producedNodeIds'][0]}"
    ).json()["mediaNode"]["mediaNode"]["id"]
    print(f"[7] 图片已出：{result_node}")

    derive = api.wait(
        api.action(f"/media-nodes/{result_node}/derive", f"demo-derive-{stamp}")
    )
    derived = [
        api.client.get(f"/canvas/nodes/{node_id}").json()["mediaNode"]["mediaNode"]["id"]
        for node_id in derive["producedNodeIds"]
    ]
    for media_node_id in derived:
        api.client.patch(f"/media-nodes/{media_node_id}", json={"blocked": True})
    print(f"[8] 立绘联合派生 {len(derived)} 张，已屏蔽多余候选")

    video_plan_response = api.client.post(
        f"/clips/{clip['id']}/video-plans",
        json={"requirement": "雨夜重逢，克制"},
        headers={"Idempotency-Key": f"demo-videoplan-{stamp}"},
    )
    if video_plan_response.status_code >= 400:
        raise SystemExit(f"视频方案失败：{video_plan_response.text}")
    video_plan_body = video_plan_response.json()
    api.wait(video_plan_body["submissionId"])
    video_plan_node = video_plan_body["mediaNode"]["id"]
    print(f"[9] 视频方案已生成并冻结参考图：{video_plan_node}")

    video = api.wait(
        api.action(f"/media/{video_plan_node}/draw", f"demo-videodraw-{stamp}", {"count": 1, "seed": -1})
    )
    video_result = api.client.get(
        f"/canvas/nodes/{video['producedNodeIds'][0]}"
    ).json()["mediaNode"]
    print(
        f"[10] 视频已出：{video_result['mediaNode']['id']} "
        f"decode={video_result['media']['decodeStatus']} "
        f"duration={video_result['media']['durationMs']}ms "
        f"thumb={video_result['media']['thumbnailUrl']}"
    )

    canvas = api.client.get(f"/episodes/{episode_id}/canvas").json()
    print(
        f"\n完成：节点 {len(canvas['nodes'])} 个，依赖边 {len(canvas['edges'])} 条。"
        f"\n前端地址：http://127.0.0.1:5173/projects/{project['project']['id']}"
    )
    print(f"PROJECT_ID={project['project']['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
