"""端到端 mock 全链路：大纲 → 人物设定 → 剧本 → Clip → 资产 → 图片 → 立绘派生 → 视频。

PRD §9 的验收 1–7、9、10（mock 部分）由这个文件覆盖。
关键点：mock 不发任何外部请求，但产出**真实可用**的文件——图片能解码、视频能解码并抽首帧。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import accept, auth_header, submission_of, wait_succeeded

OUTLINE_BODY = "两个校花女友\n林晚在雨夜校门口等了三十分钟。"


# --- 工具 ---------------------------------------------------------------


def _canvas(client: TestClient, token: str, episode_id: str) -> dict:
    return client.get(
        f"/api/v1/episodes/{episode_id}/canvas", headers=auth_header(token)
    ).json()


def _node(client: TestClient, token: str, node_id: str) -> dict:
    response = client.get(f"/api/v1/canvas/nodes/{node_id}", headers=auth_header(token))
    assert response.status_code == 200, response.text
    return response.json()


def _document_id_of_node(client: TestClient, token: str, node_id: str) -> str:
    return _node(client, token, node_id)["document"]["document"]["id"]


def _media_node_id_of_canvas_node(client: TestClient, token: str, node_id: str) -> str:
    return _node(client, token, node_id)["mediaNode"]["mediaNode"]["id"]


def _media_detail(client: TestClient, token: str, media_node_id: str) -> dict:
    response = client.get(
        f"/api/v1/media-nodes/{media_node_id}", headers=auth_header(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- 主链路 -------------------------------------------------------------


def test_full_mock_chain(client: TestClient, user_token: str, runner) -> None:
    token = user_token

    # 1) 新建项目：自动带第 1 集与大纲节点
    project = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": "死党落水后"}
    ).json()
    episode_id = project["episodes"][0]["id"]
    outline_node_id = project["outlineNodeId"]
    outline_doc_id = _document_id_of_node(client, token, outline_node_id)

    # 2) 填写大纲并保存
    client.patch(
        f"/api/v1/documents/{outline_doc_id}/draft",
        headers=auth_header(token),
        json={"body": OUTLINE_BODY, "title": "两个校花女友"},
    )
    saved = client.post(
        f"/api/v1/documents/{outline_doc_id}/revisions",
        headers=auth_header(token),
        json={"body": OUTLINE_BODY, "title": "两个校花女友"},
    )
    assert saved.status_code == 201

    # 3) 生成人物设定（进草稿区，不改写旧版本）
    setting_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{outline_doc_id}/actions/generate-setting",
            headers={**auth_header(token), "Idempotency-Key": "chain-setting-1"},
            json={"requirement": "按大纲产出主要人物"},
        ),
    )
    detail = wait_succeeded(client, token, setting_sub, runner)
    setting_doc_id = detail["result"]["settingDocumentId"]
    setting_body = client.get(
        f"/api/v1/documents/{setting_doc_id}", headers=auth_header(token)
    ).json()["document"]["body"]
    assert "林晚" in setting_body or "身份" in setting_body

    # 4) 生成剧本（大纲 + 已确认设定一起保存）
    script_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{outline_doc_id}/actions/generate-script",
            headers={**auth_header(token), "Idempotency-Key": "chain-script-1"},
            json={"requirement": "写第 1 集"},
        ),
    )
    script_detail = wait_succeeded(client, token, script_sub, runner)
    script_node_id = script_detail["producedNodeIds"][0]
    script_doc_id = _document_id_of_node(client, token, script_node_id)
    script_document = client.get(
        f"/api/v1/documents/{script_doc_id}", headers=auth_header(token)
    ).json()["document"]
    # 可追溯：剧本绑定大纲版本与设定快照
    assert script_document["boundOutlineRevisionId"]
    assert script_document["boundSettingRevisionId"]

    # 5) 拆分 Clip
    split_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{script_doc_id}/actions/split-clips",
            headers={**auth_header(token), "Idempotency-Key": "chain-split-1"},
            json={},
        ),
    )
    split_detail = wait_succeeded(client, token, split_sub, runner)
    assert len(split_detail["producedNodeIds"]) >= 2

    clips = client.get(
        f"/api/v1/episodes/{episode_id}/clips", headers=auth_header(token)
    ).json()
    assert clips["total"] >= 2
    clip = clips["items"][0]
    clip_detail = client.get(f"/api/v1/clips/{clip['id']}", headers=auth_header(token)).json()
    assert clip_detail["shots"], "Clip 必须带内部镜头"
    assert clip_detail["shots"][0]["visualText"]

    # 6) 资产规划（人物立绘）→ 确认清单
    plan_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{script_doc_id}/actions/plan-characters",
            headers={**auth_header(token), "Idempotency-Key": "chain-plan-1"},
            json={},
        ),
    )
    plan_detail = wait_succeeded(client, token, plan_sub, runner)
    plan_items = plan_detail["result"]["assets"]
    assert plan_items

    confirmed = client.post(
        "/api/v1/asset-plans/confirm",
        headers=auth_header(token),
        json={"submissionId": plan_sub, "items": plan_items},
    )
    assert confirmed.status_code == 200, confirmed.text
    confirm_body = confirmed.json()
    # 确认清单不直接生图：只创建图片方案节点
    assert confirm_body["createdMediaNodes"], confirm_body
    assert confirm_body["references"], "规范资产名要回填到 Clip 参考列表"

    plan_media_node_id = confirm_body["createdMediaNodes"][0]["id"]
    plan_detail_body = _media_detail(client, token, plan_media_node_id)
    assert plan_detail_body["visiblePrompt"]
    # 隐藏 A 与内部组装文本绝不外泄
    assert "hidden" not in plan_detail_body
    assert "finalInputText" not in plan_detail_body["promptVersions"][0] if plan_detail_body["promptVersions"] else True

    # 7) 抽卡出图
    draw_sub = accept(
        client,
        client.post(
            f"/api/v1/media/{plan_media_node_id}/draw",
            headers={**auth_header(token), "Idempotency-Key": "chain-draw-1"},
            json={"count": 1, "seed": -1},
        ),
    )
    draw_detail = wait_succeeded(client, token, draw_sub, runner)
    assert draw_detail["producedNodeIds"]
    image_result_canvas_node = draw_detail["producedNodeIds"][0]
    image_result_node_id = _media_node_id_of_canvas_node(
        client, token, image_result_canvas_node
    )
    image_result = _media_detail(client, token, image_result_node_id)
    assert image_result["media"]["decodeStatus"] == "verified"
    assert image_result["media"]["width"] > 0
    assert image_result["media"]["thumbnailUrl"]

    # 出图记一次 attempt 且交付核验通过
    assert len(draw_detail["attempts"]) == 1
    attempt = draw_detail["attempts"][0]
    assert attempt["physicalStatus"] == "succeeded"
    assert attempt["deliveryStatus"] == "verified"
    assert attempt["isMock"] is True
    # user 角色不返回任何计费字段
    assert attempt["usage"] is None

    # 8) 立绘联合派生：固定 2 个结果，各 1 张
    derive_sub = accept(
        client,
        client.post(
            f"/api/v1/media-nodes/{image_result_node_id}/derive",
            headers={**auth_header(token), "Idempotency-Key": "chain-derive-1"},
            json={},
        ),
    )
    derive_detail = wait_succeeded(client, token, derive_sub, runner)
    assert len(derive_detail["producedNodeIds"]) == 2

    derived_media_ids = [
        _media_node_id_of_canvas_node(client, token, node_id)
        for node_id in derive_detail["producedNodeIds"]
    ]
    for media_node_id in derived_media_ids:
        assert _media_detail(client, token, media_node_id)["media"]["decodeStatus"] == "verified"

    # 9) 屏蔽多余候选，保证唯一有效结果
    for media_node_id in derived_media_ids:
        blocked = client.patch(
            f"/api/v1/media-nodes/{media_node_id}",
            headers=auth_header(token),
            json={"blocked": True},
        )
        assert blocked.status_code == 200

    candidate_count = client.get(
        f"/api/v1/assets/{image_result['mediaNode']['assetId']}", headers=auth_header(token)
    ).json()["usableResultCount"]
    assert candidate_count == 1

    # 10) 生成视频方案（前置校验通过后才建节点）
    video_plan_response = client.post(
        f"/api/v1/clips/{clip['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "chain-videoplan-1"},
        json={"requirement": "雨夜重逢"},
    )
    assert video_plan_response.status_code == 202, video_plan_response.text
    video_plan_body = video_plan_response.json()
    video_plan_node_id = video_plan_body["mediaNode"]["id"]
    wait_succeeded(client, token, video_plan_body["submissionId"], runner)

    video_plan = _media_detail(client, token, video_plan_node_id)
    assert video_plan["visiblePrompt"]
    assert len(video_plan["referenceImages"]) == 1
    assert video_plan["frozenClip"]["id"] == clip["id"]
    # 产线规范 lint：提示词按真实终剪交付物的结构产出，应当零告警
    assert video_plan["sourceSummary"]["promptWarnings"] == [], video_plan["sourceSummary"]
    # 分镜标签、参考图编号、无字幕收尾都要在
    prompt_text = video_plan["visiblePrompt"]
    assert "镜头1（0-" in prompt_text
    assert "参考图一" in prompt_text
    assert "无字幕" in prompt_text

    # 11) 视频抽卡，落盘并可抽首帧
    video_draw_sub = accept(
        client,
        client.post(
            f"/api/v1/media/{video_plan_node_id}/draw",
            headers={**auth_header(token), "Idempotency-Key": "chain-videodraw-1"},
            json={"count": 1, "seed": -1},
        ),
    )
    video_draw = wait_succeeded(client, token, video_draw_sub, runner)
    video_canvas_node = video_draw["producedNodeIds"][0]
    video_result_node_id = _media_node_id_of_canvas_node(client, token, video_canvas_node)
    video_result = _media_detail(client, token, video_result_node_id)
    assert video_result["media"]["mediaType"] == "video"
    assert video_result["media"]["decodeStatus"] == "verified"
    assert video_result["media"]["durationMs"] > 0
    assert video_result["media"]["thumbnailUrl"], "视频结果必须能取到首帧缩略图"

    # 媒体文件真的能读到字节
    file_response = client.get(video_result["media"]["fileUrl"])
    assert file_response.status_code == 200
    assert len(file_response.content) > 1000
    thumb_response = client.get(video_result["media"]["thumbnailUrl"])
    assert thumb_response.status_code == 200
    assert thumb_response.headers["content-type"].startswith("image/")

    # 12) 刷新后（重新拉画布）节点、布局、引用仍在
    canvas = _canvas(client, token, episode_id)
    node_ids = {item["node"]["id"] for item in canvas["nodes"]}
    assert outline_node_id in node_ids
    assert script_node_id in node_ids
    assert video_canvas_node in node_ids
    assert all(item["layout"]["width"] > 0 for item in canvas["nodes"])
    assert canvas["edges"], "依赖边要能追溯"


def test_video_plan_requires_exactly_one_usable_result(
    client: TestClient, user_token: str, runner
) -> None:
    """0 个候选时点名拦截，并且不创建任何方案节点、不调用视频模型。"""
    token = user_token
    project = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": "前置校验"}
    ).json()
    episode_id = project["episodes"][0]["id"]

    clip = client.post(
        f"/api/v1/episodes/{episode_id}/clips",
        headers=auth_header(token),
        json={"title": "没有参考资产", "clipNo": 1},
    ).json()["clip"]

    before = _canvas(client, token, episode_id)["nodes"]

    response = client.post(
        f"/api/v1/clips/{clip['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "no-asset-1"},
        json={},
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "ASSET_REFERENCE_INVALID"

    after = _canvas(client, token, episode_id)["nodes"]
    assert len(after) == len(before), "校验失败不得创建视频方案节点"


def test_duplicate_idempotency_key_reuses_submission(
    client: TestClient, user_token: str, runner
) -> None:
    token = user_token
    project = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": "幂等"}
    ).json()
    outline_doc_id = _document_id_of_node(client, token, project["outlineNodeId"])
    client.post(
        f"/api/v1/documents/{outline_doc_id}/revisions",
        headers=auth_header(token),
        json={"body": "大纲", "title": "大纲"},
    )

    headers = {**auth_header(token), "Idempotency-Key": "same-key-1"}
    first = client.post(
        f"/api/v1/documents/{outline_doc_id}/actions/optimize", headers=headers, json={}
    )
    second = client.post(
        f"/api/v1/documents/{outline_doc_id}/actions/optimize", headers=headers, json={}
    )
    assert first.json()["submissionId"] == second.json()["submissionId"]

    # 同一个 key 换成不同请求体 → 409
    conflict = client.post(
        f"/api/v1/documents/{outline_doc_id}/actions/optimize",
        headers=headers,
        json={"requirement": "不同的需求"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_missing_idempotency_key_is_rejected(client: TestClient, user_token: str) -> None:
    token = user_token
    project = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": "缺 key"}
    ).json()
    response = client.post(
        f"/api/v1/documents/{_document_id_of_node(client, token, project['outlineNodeId'])}/actions/optimize",
        headers=auth_header(token),
        json={},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_generate_requires_saved_revision(
    client: TestClient, user_token: str, runner
) -> None:
    token = user_token
    project = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": "未保存"}
    ).json()
    document_id = _document_id_of_node(client, token, project["outlineNodeId"])
    client.patch(
        f"/api/v1/documents/{document_id}/draft",
        headers=auth_header(token),
        json={"body": "只有草稿"},
    )
    # 未保存成版本前不能发送优化
    accept(
        client,
        client.post(
            f"/api/v1/documents/{document_id}/actions/optimize",
            headers={**auth_header(token), "Idempotency-Key": "unsaved-1"},
            json={},
        ),
    )
    runner()
    detail = submission_of(
        client,
        token,
        client.get(
            f"/api/v1/episodes/{project['episodes'][0]['id']}/submissions",
            headers=auth_header(token),
        ).json()["items"][0]["id"],
    )
    assert detail["submission"]["logicalStatus"] == "failed"
    assert "保存" in detail["submission"]["errorMessage"]
