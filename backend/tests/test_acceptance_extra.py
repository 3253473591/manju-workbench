"""PRD §9 验收里靠「设计保证」还不够、必须写成断言的两条：

- 第 4 条：更换立绘再次联合派生会创建新结果，**旧结果与其源图记录不受影响**
- 第 7 条：修改 Clip 后重新生成得到新方案节点，**旧方案重抽仍使用旧的冻结上下文**
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import accept, auth_header, wait_succeeded

OUTLINE = "两个校花女友\n\n林晚在雨夜校门口等了三十分钟。"


def _document_id(client: TestClient, token: str, node_id: str) -> str:
    return client.get(f"/api/v1/canvas/nodes/{node_id}", headers=auth_header(token)).json()[
        "document"
    ]["document"]["id"]


def _media_node_id(client: TestClient, token: str, canvas_node_id: str) -> str:
    return client.get(
        f"/api/v1/canvas/nodes/{canvas_node_id}", headers=auth_header(token)
    ).json()["mediaNode"]["mediaNode"]["id"]


def _media_node(client: TestClient, token: str, media_node_id: str) -> dict:
    return client.get(
        f"/api/v1/media-nodes/{media_node_id}", headers=auth_header(token)
    ).json()


def _stamp(prefix: str, index: int) -> dict[str, str]:
    return {"Idempotency-Key": f"{prefix}-{index}"}


def prepare_image_stage(
    client: TestClient, token: str, runner, *, draws: int = 1, prefix: str = "stage"
) -> dict:
    """把链路推进到「已有图片结果」这一步，返回后续用例需要的 id。"""
    project = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": f"验收 {prefix}"}
    ).json()
    episode_id = project["episodes"][0]["id"]
    outline_doc = _document_id(client, token, project["outlineNodeId"])

    client.patch(
        f"/api/v1/documents/{outline_doc}/draft",
        headers=auth_header(token),
        json={"body": OUTLINE, "title": "两个校花女友"},
    )
    client.post(
        f"/api/v1/documents/{outline_doc}/revisions",
        headers=auth_header(token),
        json={"body": OUTLINE, "title": "两个校花女友"},
    )

    accept(
        client,
        client.post(
            f"/api/v1/documents/{outline_doc}/actions/generate-setting",
            headers={**auth_header(token), **_stamp(f"{prefix}-set", 1)},
            json={},
        ),
    )
    runner()

    script_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{outline_doc}/actions/generate-script",
            headers={**auth_header(token), **_stamp(f"{prefix}-script", 1)},
            json={},
        ),
    )
    script_detail = wait_succeeded(client, token, script_sub, runner)
    script_doc = _document_id(client, token, script_detail["producedNodeIds"][0])

    accept(
        client,
        client.post(
            f"/api/v1/documents/{script_doc}/actions/split-clips",
            headers={**auth_header(token), **_stamp(f"{prefix}-split", 1)},
            json={},
        ),
    )
    runner()
    clip = client.get(
        f"/api/v1/episodes/{episode_id}/clips", headers=auth_header(token)
    ).json()["items"][0]

    plan_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{script_doc}/actions/plan-characters",
            headers={**auth_header(token), **_stamp(f"{prefix}-plan", 1)},
            json={},
        ),
    )
    plan_detail = wait_succeeded(client, token, plan_sub, runner)
    confirmed = client.post(
        "/api/v1/asset-plans/confirm",
        headers=auth_header(token),
        json={"submissionId": plan_sub, "items": plan_detail["result"]["assets"]},
    ).json()
    plan_node = confirmed["createdMediaNodes"][0]["id"]

    results: list[str] = []
    for index in range(draws):
        draw = wait_succeeded(
            client,
            token,
            accept(
                client,
                client.post(
                    f"/api/v1/media/{plan_node}/draw",
                    headers={**auth_header(token), **_stamp(f"{prefix}-draw", index)},
                    json={"count": 1, "seed": -1},
                ),
            ),
            runner,
        )
        results.append(_media_node_id(client, token, draw["producedNodeIds"][0]))

    return {
        "project": project,
        "episodeId": episode_id,
        "clip": clip,
        "scriptDocId": script_doc,
        "planNode": plan_node,
        "results": results,
    }


def prepare_script_stage(
    client: TestClient, token: str, runner, *, prefix: str = "scriptstage"
) -> dict:
    """推进到「剧本已拆出 Clip」这一步，供资产规划相关的用例使用。"""
    project = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": f"验收 {prefix}"}
    ).json()
    episode_id = project["episodes"][0]["id"]
    outline_doc = _document_id(client, token, project["outlineNodeId"])

    client.patch(
        f"/api/v1/documents/{outline_doc}/draft",
        headers=auth_header(token),
        json={"body": OUTLINE, "title": "两个校花女友"},
    )
    client.post(
        f"/api/v1/documents/{outline_doc}/revisions",
        headers=auth_header(token),
        json={"body": OUTLINE, "title": "两个校花女友"},
    )
    accept(
        client,
        client.post(
            f"/api/v1/documents/{outline_doc}/actions/generate-setting",
            headers={**auth_header(token), **_stamp(f"{prefix}-set", 1)},
            json={},
        ),
    )
    runner()
    script_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{outline_doc}/actions/generate-script",
            headers={**auth_header(token), **_stamp(f"{prefix}-script", 1)},
            json={},
        ),
    )
    script_detail = wait_succeeded(client, token, script_sub, runner)
    script_doc = _document_id(client, token, script_detail["producedNodeIds"][0])
    accept(
        client,
        client.post(
            f"/api/v1/documents/{script_doc}/actions/split-clips",
            headers={**auth_header(token), **_stamp(f"{prefix}-split", 1)},
            json={},
        ),
    )
    runner()
    clips = client.get(
        f"/api/v1/episodes/{episode_id}/clips", headers=auth_header(token)
    ).json()["items"]
    return {
        "projectId": project["project"]["id"],
        "episodeId": episode_id,
        "scriptDocId": script_doc,
        "clips": clips,
    }


def test_confirm_plan_normalizes_model_vocabulary_and_drops_fake_clip_ids(
    client: TestClient, user_token: str, runner
) -> None:
    """真实调用暴露过的两个坑，必须长期锁死：

    1. 模型会用中文或自创英文（实测返回过 `assetType="人物"`、`imageKind="角色立绘"`），
       **不能拿它直接构造枚举**；
    2. 模型会把 Clip id 编出来（实测返回 `clip_001`…），
       **绝不能拿它去反查项目**，更不能找不到就退回到「库里的第一个项目」。
    """
    token = user_token
    stage = prepare_script_stage(client, token, runner, prefix="fakeclip")
    assert stage["clips"], "前置条件：需要已拆出的 Clip"

    # 先建一个真实的规划提交，作为权威范围来源
    plan_sub = accept(
        client,
        client.post(
            f"/api/v1/documents/{stage['scriptDocId']}/actions/plan-characters",
            headers={**auth_header(token), **_stamp("fakeclip-plan", 1)},
            json={},
        ),
    )
    runner()

    fabricated = [
        {
            # 故意用模型实测过的「外语」：中文枚举 + 幻觉 Clip id
            "assetType": "人物",
            "canonicalName": "林晚",
            "variantName": "默认造型",
            "imageKind": "角色立绘",
            "clipIds": ["clip_001", "clip_002", "clip_003"],
        },
        {
            "assetType": "场景",
            "canonicalName": "雨夜校门",
            "variantName": "",
            "imageKind": "场景图",
            "clipIds": ["clip_001"],
        },
    ]

    response = client.post(
        "/api/v1/asset-plans/confirm",
        headers=auth_header(token),
        json={"submissionId": plan_sub, "items": fabricated},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # 假 Clip id 全部丢弃并如实回报
    assert set(body["droppedClipIds"]) == {"clip_001", "clip_002", "clip_003"}
    # 资产落在正确的项目里
    assert {asset["assetType"] for asset in body["assets"]} == {"character", "scene"}
    assert all(asset["projectId"] == stage["projectId"] for asset in body["assets"])
    # 中文图种被归一成内部取值
    character = next(a for a in body["assets"] if a["assetType"] == "character")
    assert character["imageKind"] == "portrait"
    scene = next(a for a in body["assets"] if a["assetType"] == "scene")
    assert scene["imageKind"] == "scene"

    # 引用被回填到真实的 Clip 上（而不是幻觉 id）
    real_clip_ids = {clip["id"] for clip in stage["clips"]}
    referenced = {reference["clipId"] for reference in body["references"]}
    assert referenced
    assert referenced <= real_clip_ids

    # 资产方案节点建边时走了系统豁免，不会被「手动拖线只允许同级或下一阶段」拦下
    assert body["createdMediaNodes"], "应当为每个资产创建图片方案节点"


def test_confirm_plan_requires_authoritative_scope(client: TestClient, user_token: str) -> None:
    """没有来源提交、又给不出合法 Clip id 时，必须报错而不是猜一个项目。"""
    token = user_token
    response = client.post(
        "/api/v1/asset-plans/confirm",
        headers=auth_header(token),
        json={
            "items": [
                {
                    "assetType": "character",
                    "canonicalName": "幽灵角色",
                    "clipIds": ["clip_001"],
                }
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BUSINESS_RULE_VIOLATION"


def test_asset_type_alias_helper() -> None:
    from app.models.enums import AssetType
    from app.services import naming

    assert naming.parse_asset_type("人物") is AssetType.character
    assert naming.parse_asset_type("角色") is AssetType.character
    assert naming.parse_asset_type("Person") is AssetType.character
    assert naming.parse_asset_type("场景") is AssetType.scene
    assert naming.parse_asset_type("载具") is AssetType.prop
    assert naming.parse_asset_type("完全看不懂的东西") is None
    assert naming.parse_image_kind("角色立绘") == "portrait"
    assert naming.parse_image_kind("full_body_reference") == "portrait"
    assert naming.parse_image_kind("三视图") == "three_view"


def test_video_draw_rejects_duration_beyond_tier_before_calling_provider(
    client: TestClient, user_token: str, runner
) -> None:
    """手册 §4.0：mini / fast 只支持 4–15 秒，>15s 只有 2.5 档能做。

    不支持时必须在**提交前**报错并提示用户改档，不能静默截断 Clip，
    也不能白跑一次外部调用。
    """
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="duration")

    # 把 Clip 改成 20 秒
    patched = client.patch(
        f"/api/v1/clips/{stage['clip']['id']}",
        headers=auth_header(token),
        json={"durationMs": 20000},
    )
    assert patched.status_code == 200, patched.text

    plan_response = client.post(
        f"/api/v1/clips/{stage['clip']['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "duration-plan"},
        json={},
    )
    assert plan_response.status_code == 202, plan_response.text
    body = plan_response.json()
    plan_node = body["mediaNode"]["id"]
    wait_succeeded(client, token, body["submissionId"], runner)

    # 方案里如实记着 20 秒，没有被截断成 15
    assert body["mediaNode"]["params"]["durationSeconds"] == 20

    draw = accept(
        client,
        client.post(
            f"/api/v1/media/{plan_node}/draw",
            headers={**auth_header(token), "Idempotency-Key": "duration-draw"},
            json={"count": 1, "seed": -1, "productLabel": "mini"},
        ),
    )
    runner()
    detail = client.get(f"/api/v1/submissions/{draw}", headers=auth_header(token)).json()
    assert detail["submission"]["logicalStatus"] == "failed"
    assert detail["submission"]["errorCode"] == "MODEL_NOT_AVAILABLE"
    assert "4–15" in detail["submission"]["errorMessage"]
    # 失败发生在外部调用之前：没有产生任何 attempt
    assert detail["attempts"] == []
    assert detail["producedNodeIds"] == []


def test_video_plan_prompt_lint_flags_bad_prompt(
    client: TestClient, user_token: str, runner, monkeypatch
) -> None:
    """提示词一旦违反产线规范，必须当场把问题挂到方案节点上，而不是拿去试错烧钱。"""
    from app.providers.mock import MockTextProvider

    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="lint")

    def bad_generate(self, *, system_prompt, user_prompt, expect="text", json_mode=True, context=None):
        from app.providers.base import TextResult

        # 缺分镜标签、缺参考图编号、缺无字幕，还带了严禁措辞
        text = {"visibleText": "雨夜重逢，两人站定。全片与参考图保持一致。"}
        import json as _json

        return TextResult(
            text=_json.dumps(text, ensure_ascii=False),
            input_tokens=1,
            output_tokens=1,
            provider_request_id="mock_bad",
            raw_meta={"mock": True},
        )

    monkeypatch.setattr(MockTextProvider, "generate", bad_generate)

    plan_response = client.post(
        f"/api/v1/clips/{stage['clip']['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "lint-plan"},
        json={},
    )
    assert plan_response.status_code == 202, plan_response.text
    body = plan_response.json()
    wait_succeeded(client, token, body["submissionId"], runner)

    detail = _media_node(client, token, body["mediaNode"]["id"])
    warnings = detail["sourceSummary"]["promptWarnings"]
    codes = {warning["code"] for warning in warnings}
    assert "PROMPT_MISSING_SHOT_LABELS" in codes
    assert "PROMPT_MISSING_REFERENCE_LABELS" in codes
    assert "PROMPT_MISSING_NO_SUBTITLE" in codes
    assert "PROMPT_FORBIDDEN_PHRASE" in codes


def test_derive_twice_creates_new_results_and_keeps_old_source(
    client: TestClient, user_token: str, runner
) -> None:
    """PRD §9-4：更换立绘再次派生，创建新结果；旧结果与其源图记录不变。"""
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=2, prefix="derive2")
    first_portrait, second_portrait = stage["results"]

    first_derive = wait_succeeded(
        client,
        token,
        accept(
            client,
            client.post(
                f"/api/v1/media-nodes/{first_portrait}/derive",
                headers={**auth_header(token), "Idempotency-Key": "derive-first"},
                json={},
            ),
        ),
        runner,
    )
    first_nodes = [_media_node_id(client, token, node_id) for node_id in first_derive["producedNodeIds"]]
    first_before = {node_id: _media_node(client, token, node_id) for node_id in first_nodes}
    assert len(first_nodes) == 2, "联合派生固定 2 个结果节点"

    second_derive = wait_succeeded(
        client,
        token,
        accept(
            client,
            client.post(
                f"/api/v1/media-nodes/{second_portrait}/derive",
                headers={**auth_header(token), "Idempotency-Key": "derive-second"},
                json={},
            ),
        ),
        runner,
    )
    second_nodes = [
        _media_node_id(client, token, node_id) for node_id in second_derive["producedNodeIds"]
    ]

    # 新批次是独立的节点，不覆盖旧批次
    assert len(second_nodes) == 2
    assert set(first_nodes).isdisjoint(set(second_nodes))

    # 旧结果的内容与源图记录一字未改
    for node_id in first_nodes:
        after = _media_node(client, token, node_id)
        assert after["mediaNode"] == first_before[node_id]["mediaNode"]
        assert after["media"]["id"] == first_before[node_id]["media"]["id"]

    # 两个批次各自记着自己的源图
    first_source = _media_node(client, token, first_nodes[0])["mediaNode"]["sourceMediaId"]
    second_source = _media_node(client, token, second_nodes[0])["mediaNode"]["sourceMediaId"]
    assert first_source != second_source


def test_new_video_plan_does_not_rewrite_old_frozen_context(
    client: TestClient, user_token: str, runner
) -> None:
    """PRD §9-7：改 Clip 再生成得到新方案节点；旧方案重抽仍用旧冻结上下文。"""
    token = user_token
    stage = prepare_image_stage(client, token, runner, draws=1, prefix="freeze")
    clip = stage["clip"]

    first_response = client.post(
        f"/api/v1/clips/{clip['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "vplan-first"},
        json={"requirement": "第一版"},
    )
    assert first_response.status_code == 202, first_response.text
    first_body = first_response.json()
    first_plan = first_body["mediaNode"]["id"]
    wait_succeeded(client, token, first_body["submissionId"], runner)

    frozen_before = _media_node(client, token, first_plan)
    assert frozen_before["frozenClip"]["title"] == clip["title"]
    assert len(frozen_before["referenceImages"]) == 1

    # 修改 Clip 后重新生成视频方案
    patched = client.patch(
        f"/api/v1/clips/{clip['id']}",
        headers=auth_header(token),
        json={"title": "改过标题的 Clip", "expectedVersion": clip["version"]},
    )
    assert patched.status_code == 200, patched.text

    second_response = client.post(
        f"/api/v1/clips/{clip['id']}/video-plans",
        headers={**auth_header(token), "Idempotency-Key": "vplan-second"},
        json={"requirement": "第二版"},
    )
    assert second_response.status_code == 202, second_response.text
    second_body = second_response.json()
    second_plan = second_body["mediaNode"]["id"]
    assert second_plan != first_plan, "重新生成必须创建新的方案节点"
    wait_succeeded(client, token, second_body["submissionId"], runner)

    # 旧方案的冻结上下文保持原样
    frozen_after = _media_node(client, token, first_plan)
    assert frozen_after["frozenClip"]["title"] == clip["title"]
    assert frozen_after["frozenClip"] == frozen_before["frozenClip"]
    assert frozen_after["referenceImages"] == frozen_before["referenceImages"]

    # 新方案才反映修改后的 Clip
    new_plan = _media_node(client, token, second_plan)
    assert new_plan["frozenClip"]["title"] == "改过标题的 Clip"

    # 旧方案重抽：attempt 的输入快照仍然引用旧的冻结上下文
    draw = wait_succeeded(
        client,
        token,
        accept(
            client,
            client.post(
                f"/api/v1/media/{first_plan}/draw",
                headers={**auth_header(token), "Idempotency-Key": "vplan-first-draw"},
                json={"count": 1, "seed": -1},
            ),
        ),
        runner,
    )
    assert draw["producedNodeIds"], "旧方案应当还能出片"
    assert draw["attempts"][0]["deliveryStatus"] == "verified"

    # 旧方案节点上的冻结参考图数量不变
    assert len(_media_node(client, token, first_plan)["referenceImages"]) == 1
