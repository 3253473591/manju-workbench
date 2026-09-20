"""Project 与 Episode：新建项目自动铺开第 1 集与大纲节点。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import auth_header


def create_project(client: TestClient, token: str, name: str = "死党落水后") -> dict:
    response = client.post(
        "/api/v1/projects",
        headers=auth_header(token),
        json={"name": name, "description": "雨夜重逢"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_project_bootstraps_episode_and_outline_node(
    client: TestClient, user_token: str
) -> None:
    detail = create_project(client, user_token)
    assert detail["project"]["name"] == "死党落水后"
    assert detail["project"]["status"] == "active"

    # 每项目自动创建「第 1 集」
    assert len(detail["episodes"]) == 1
    episode = detail["episodes"][0]
    assert episode["number"] == 1
    assert episode["title"] == "第 1 集"
    assert episode["projectId"] == detail["project"]["id"]
    assert detail["outlineNodeId"]


def test_outline_node_is_text_node_bound_to_outline_document(
    client: TestClient, user_token: str
) -> None:
    from sqlmodel import select

    from app.db.engine import session_scope
    from app.models.canvas import CanvasNode, CanvasNodeLayout
    from app.models.content import ContentDocument
    from app.models.enums import CanvasNodeType, DocumentKind

    detail = create_project(client, user_token)
    node_id = detail["outlineNodeId"]

    with session_scope() as db:
        node = db.exec(select(CanvasNode).where(CanvasNode.id == node_id)).one()
        assert node.node_type == CanvasNodeType.text
        assert node.hidden is False

        document = db.exec(
            select(ContentDocument).where(ContentDocument.id == node.document_id)
        ).one()
        assert document.kind == DocumentKind.outline
        assert document.episode_id == detail["episodes"][0]["id"]
        # 文档与节点互相引用，两边都要对得上
        assert document.node_id == node.id

        layout = db.exec(
            select(CanvasNodeLayout).where(CanvasNodeLayout.node_id == node_id)
        ).one()
        assert layout.width > 0 and layout.height > 0


def test_list_and_get_project(client: TestClient, user_token: str) -> None:
    created = create_project(client, user_token, "项目 A")
    create_project(client, user_token, "项目 B")

    listing = client.get("/api/v1/projects", headers=auth_header(user_token)).json()
    assert listing["total"] == 2
    assert listing["page"] == 1
    assert len(listing["items"]) == 2

    detail = client.get(
        f"/api/v1/projects/{created['project']['id']}", headers=auth_header(user_token)
    ).json()
    assert detail["project"]["name"] == "项目 A"
    assert detail["outlineNodeId"] == created["outlineNodeId"]


def test_patch_project(client: TestClient, user_token: str) -> None:
    created = create_project(client, user_token)
    project_id = created["project"]["id"]

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=auth_header(user_token),
        json={"name": "新名字", "status": "archived"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project"]["name"] == "新名字"
    assert body["project"]["status"] == "archived"


def test_patch_project_rejects_bad_status(client: TestClient, user_token: str) -> None:
    created = create_project(client, user_token)
    response = client.patch(
        f"/api/v1/projects/{created['project']['id']}",
        headers=auth_header(user_token),
        json={"status": "not-a-status"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_delete_project_soft_deletes_episodes(client: TestClient, user_token: str) -> None:
    created = create_project(client, user_token)
    project_id = created["project"]["id"]
    episode_id = created["episodes"][0]["id"]

    assert (
        client.delete(
            f"/api/v1/projects/{project_id}", headers=auth_header(user_token)
        ).status_code
        == 204
    )
    assert (
        client.get(
            f"/api/v1/projects/{project_id}", headers=auth_header(user_token)
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/episodes/{episode_id}", headers=auth_header(user_token)
        ).status_code
        == 404
    )
    # 列表里也不再出现
    listing = client.get("/api/v1/projects", headers=auth_header(user_token)).json()
    assert listing["total"] == 0


def test_episode_endpoints(client: TestClient, user_token: str) -> None:
    created = create_project(client, user_token)
    project_id = created["project"]["id"]

    episodes = client.get(
        f"/api/v1/projects/{project_id}/episodes", headers=auth_header(user_token)
    ).json()
    assert episodes["total"] == 1

    episode_id = episodes["items"][0]["id"]
    single = client.get(
        f"/api/v1/episodes/{episode_id}", headers=auth_header(user_token)
    ).json()
    assert single["episode"]["id"] == episode_id


def test_missing_project_returns_404(client: TestClient, user_token: str) -> None:
    response = client.get("/api/v1/projects/does-not-exist", headers=auth_header(user_token))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_request_id_is_echoed_and_generated(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-Id": "req_fixed_123"})
    assert response.headers["X-Request-Id"] == "req_fixed_123"

    generated = client.get("/api/v1/health")
    assert generated.headers["X-Request-Id"].startswith("req_")


def test_health_and_meta_are_public(client: TestClient) -> None:
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["mode"] == "mock"

    meta = client.get("/api/v1/meta")
    assert meta.status_code == 200
    body = meta.json()
    assert body["mode"] == "mock"
    # 没有任何真实调用核验过，因此真实档位不可用
    assert body["realAvailable"] is False
    assert body["imageProducts"] == ["lite", "pro"]
    assert body["videoProducts"] == ["mini", "fast", "2.5"]
