"""画布：布局、屏蔽、依赖边与节点创建校验。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import auth_header


def bootstrap(client: TestClient, token: str) -> dict:
    response = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": "画布测试"}
    )
    return response.json()


def test_canvas_snapshot_contains_outline_node_and_layout(
    client: TestClient, user_token: str
) -> None:
    project = bootstrap(client, user_token)
    episode_id = project["episodes"][0]["id"]

    canvas = client.get(
        f"/api/v1/episodes/{episode_id}/canvas", headers=auth_header(user_token)
    ).json()
    assert canvas["episodeId"] == episode_id
    assert len(canvas["nodes"]) == 1
    item = canvas["nodes"][0]
    assert item["node"]["nodeType"] == "text"
    assert item["node"]["id"] == project["outlineNodeId"]
    assert item["layout"]["width"] > 0
    assert canvas["edges"] == []


def test_layout_save_does_not_touch_content(client: TestClient, user_token: str) -> None:
    project = bootstrap(client, user_token)
    node_id = project["outlineNodeId"]
    token = user_token

    before = client.get(f"/api/v1/canvas/nodes/{node_id}", headers=auth_header(token)).json()
    document_id = before["document"]["document"]["id"]
    client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(token),
        json={"body": "正文", "title": "大纲"},
    )
    version_before = client.get(
        f"/api/v1/documents/{document_id}", headers=auth_header(token)
    ).json()["document"]["version"]
    fingerprint_before = client.get(
        f"/api/v1/canvas/nodes/{node_id}", headers=auth_header(token)
    ).json()["node"]["contentFingerprint"]

    moved = client.patch(
        f"/api/v1/canvas/nodes/{node_id}/layout",
        headers=auth_header(token),
        json={"x": 320.5, "y": -140.25, "width": 480, "height": 360, "zIndex": 3},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["layout"]["x"] == 320.5
    assert moved.json()["layout"]["zIndex"] == 3

    after = client.get(f"/api/v1/canvas/nodes/{node_id}", headers=auth_header(token)).json()
    assert after["document"]["document"]["version"] == version_before
    assert after["node"]["contentFingerprint"] == fingerprint_before


def test_layout_rejects_out_of_range_size(client: TestClient, user_token: str) -> None:
    project = bootstrap(client, user_token)
    response = client.patch(
        f"/api/v1/canvas/nodes/{project['outlineNodeId']}/layout",
        headers=auth_header(user_token),
        json={"x": 0, "y": 0, "width": 10, "height": 10},
    )
    assert response.status_code in (400, 422)
    assert response.json()["error"]["code"] in {"CANVAS_LAYOUT_INVALID", "INVALID_REQUEST"}


def test_visibility_toggle_keeps_content(client: TestClient, user_token: str) -> None:
    project = bootstrap(client, user_token)
    node_id = project["outlineNodeId"]

    hidden = client.patch(
        f"/api/v1/canvas/nodes/{node_id}/visibility",
        headers=auth_header(user_token),
        json={"hidden": True},
    )
    assert hidden.status_code == 200
    assert hidden.json()["node"]["hidden"] is True

    shown = client.patch(
        f"/api/v1/canvas/nodes/{node_id}/visibility",
        headers=auth_header(user_token),
        json={"hidden": False},
    )
    assert shown.json()["node"]["hidden"] is False


def test_create_node_requires_matching_reference(client: TestClient, user_token: str) -> None:
    project = bootstrap(client, user_token)
    episode_id = project["episodes"][0]["id"]

    # 图片节点必须给 mediaId
    response = client.post(
        f"/api/v1/episodes/{episode_id}/nodes",
        headers=auth_header(user_token),
        json={"nodeType": "image"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "NODE_TYPE_INVALID"

    bad_type = client.post(
        f"/api/v1/episodes/{episode_id}/nodes",
        headers=auth_header(user_token),
        json={"nodeType": "sound", "mediaId": "x"},
    )
    assert bad_type.status_code == 400


def test_edge_rules(client: TestClient, user_token: str) -> None:
    """拖线只允许同级或下一阶段；视频不手动同级拖线。"""
    project = bootstrap(client, user_token)
    episode_id = project["episodes"][0]["id"]
    token = user_token

    outline_node = project["outlineNodeId"]
    clip = client.post(
        f"/api/v1/episodes/{episode_id}/clips",
        headers=auth_header(token),
        json={"title": "雨夜重逢", "clipNo": 2},
    ).json()["clip"]
    clip_node = client.get(
        f"/api/v1/episodes/{episode_id}/canvas", headers=auth_header(token)
    ).json()
    shot_node_id = next(
        item["node"]["id"] for item in clip_node["nodes"] if item["node"]["clipId"] == clip["id"]
    )

    # 文本 → 分镜：下一阶段，允许
    ok = client.post(
        f"/api/v1/episodes/{episode_id}/edges",
        headers=auth_header(token),
        json={"fromNodeId": outline_node, "toNodeId": shot_node_id},
    )
    assert ok.status_code == 201, ok.text

    # 分镜 → 文本：逆向，拒绝
    reverse = client.post(
        f"/api/v1/episodes/{episode_id}/edges",
        headers=auth_header(token),
        json={"fromNodeId": shot_node_id, "toNodeId": outline_node},
    )
    assert reverse.status_code == 400
    assert reverse.json()["error"]["code"] == "NODE_TYPE_INVALID"


def test_new_nodes_avoid_existing_rectangles(client: TestClient, user_token: str) -> None:
    project = bootstrap(client, user_token)
    episode_id = project["episodes"][0]["id"]
    token = user_token

    for index in range(3):
        clip = client.post(
            f"/api/v1/episodes/{episode_id}/clips",
            headers=auth_header(token),
            json={"title": f"CLIP {index}", "clipNo": index + 10},
        ).json()["clip"]
        assert clip["id"]

    canvas = client.get(
        f"/api/v1/episodes/{episode_id}/canvas", headers=auth_header(token)
    ).json()
    rects = [
        (item["layout"]["x"], item["layout"]["y"], item["layout"]["width"], item["layout"]["height"])
        for item in canvas["nodes"]
    ]
    for index, (x, y, width, height) in enumerate(rects):
        for other_index, (ox, oy, ow, oh) in enumerate(rects):
            if index >= other_index:
                continue
            overlaps = x < ox + ow and ox < x + width and y < oy + oh and oy < y + height
            # 大纲节点与分镜同列是允许的（不同阶段），只要求同阶段不重叠
            assert not overlaps or index == 0 or other_index == 0, (
                "同批新节点不允许互相重叠"
            )


def test_delete_node_is_soft(client: TestClient, user_token: str) -> None:
    project = bootstrap(client, user_token)
    episode_id = project["episodes"][0]["id"]
    clip = client.post(
        f"/api/v1/episodes/{episode_id}/clips",
        headers=auth_header(user_token),
        json={"title": "待删", "clipNo": 99},
    ).json()["clip"]
    canvas = client.get(
        f"/api/v1/episodes/{episode_id}/canvas", headers=auth_header(user_token)
    ).json()
    node_id = next(
        item["node"]["id"] for item in canvas["nodes"] if item["node"]["clipId"] == clip["id"]
    )

    assert (
        client.delete(f"/api/v1/canvas/nodes/{node_id}", headers=auth_header(user_token)).status_code
        == 204
    )
    assert (
        client.get(f"/api/v1/canvas/nodes/{node_id}", headers=auth_header(user_token)).status_code
        == 404
    )
    # Clip 本身还在（软删除只影响画布节点）
    assert (
        client.get(f"/api/v1/clips/{clip['id']}", headers=auth_header(user_token)).status_code
        == 200
    )
