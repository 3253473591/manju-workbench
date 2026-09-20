"""内容版本：草稿/不可变版本分离、乐观锁、只在实际修改时新增版本。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import auth_header


def make_project(client: TestClient, token: str) -> dict:
    response = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": "版本测试"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def outline_document_id(client: TestClient, token: str, node_id: str) -> str:
    detail = client.get(
        f"/api/v1/canvas/nodes/{node_id}", headers=auth_header(token)
    ).json()
    return detail["document"]["document"]["id"]


def test_first_save_creates_v1(client: TestClient, user_token: str) -> None:
    project = make_project(client, user_token)
    document_id = outline_document_id(client, user_token, project["outlineNodeId"])

    saved = client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(user_token),
        json={"body": "两个校花女友，第 1 集大纲。", "title": "大纲"},
    )
    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["currentRevision"]["revisionNo"] == 1
    assert body["currentRevision"]["body"] == "两个校花女友，第 1 集大纲。"
    assert body["hasUnsavedDraft"] is False


def test_saving_identical_content_does_not_create_version(
    client: TestClient, user_token: str
) -> None:
    project = make_project(client, user_token)
    document_id = outline_document_id(client, user_token, project["outlineNodeId"])
    payload = {"body": "同一份正文", "title": "大纲"}

    assert (
        client.post(
            f"/api/v1/documents/{document_id}/revisions",
            headers=auth_header(user_token),
            json=payload,
        ).status_code
        == 201
    )
    # 内容没变 → 复用当前版本，返回 200 且不新增
    again = client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(user_token),
        json=payload,
    )
    assert again.status_code == 200, again.text
    assert len(again.json()["revisions"]) == 1


def test_draft_edit_then_save_creates_user_modified_version(
    client: TestClient, user_token: str
) -> None:
    project = make_project(client, user_token)
    document_id = outline_document_id(client, user_token, project["outlineNodeId"])
    client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(user_token),
        json={"body": "v1 正文", "title": "大纲"},
    )

    draft = client.patch(
        f"/api/v1/documents/{document_id}/draft",
        headers=auth_header(user_token),
        json={"body": "v2 草稿正文"},
    )
    assert draft.status_code == 200
    assert draft.json()["hasUnsavedDraft"] is True
    # 草稿不产生新版本
    assert len(draft.json()["revisions"]) == 1

    saved = client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(user_token),
        json={"source": "user"},
    )
    assert saved.status_code == 201
    body = saved.json()
    assert body["currentRevision"]["revisionNo"] == 2
    assert body["currentRevision"]["source"] == "user"
    assert body["hasUnsavedDraft"] is False


def test_version_conflict_returns_409(client: TestClient, user_token: str) -> None:
    project = make_project(client, user_token)
    document_id = outline_document_id(client, user_token, project["outlineNodeId"])
    detail = client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(user_token),
        json={"body": "v1", "title": "大纲"},
    ).json()
    current_version = detail["document"]["version"]

    # 另一个客户端先改了
    client.patch(
        f"/api/v1/documents/{document_id}/draft",
        headers=auth_header(user_token),
        json={"body": "别人改的"},
    )

    conflict = client.patch(
        f"/api/v1/documents/{document_id}/draft",
        headers=auth_header(user_token),
        json={"body": "我改的", "expectedVersion": current_version},
    )
    assert conflict.status_code == 409
    error = conflict.json()["error"]
    assert error["code"] == "CONTENT_VERSION_CONFLICT"
    assert error["details"]["actualVersion"] > error["details"]["expectedVersion"]


def test_switching_history_does_not_create_version(
    client: TestClient, user_token: str
) -> None:
    project = make_project(client, user_token)
    document_id = outline_document_id(client, user_token, project["outlineNodeId"])
    client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(user_token),
        json={"body": "第一版", "title": "大纲"},
    )
    client.patch(
        f"/api/v1/documents/{document_id}/draft",
        headers=auth_header(user_token),
        json={"body": "第二版"},
    )
    second = client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(user_token),
        json={"source": "user"},
    ).json()
    revisions = second["revisions"]
    assert len(revisions) == 2
    first_revision_id = revisions[-1]["id"]

    switched = client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(user_token),
        json={"fromRevisionId": first_revision_id},
    )
    assert switched.status_code == 200
    body = switched.json()
    # 切换历史版本不新增版本，只把草稿与当前指针切过去
    assert len(body["revisions"]) == 2
    assert body["document"]["body"] == "第一版"
    assert body["currentRevision"]["id"] == first_revision_id


def test_revision_list_is_readable_by_manager(
    client: TestClient, user_token: str, manager_token: str
) -> None:
    project = make_project(client, user_token)
    document_id = outline_document_id(client, user_token, project["outlineNodeId"])
    client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(user_token),
        json={"body": "正文", "title": "大纲"},
    )
    response = client.get(
        f"/api/v1/documents/{document_id}/revisions", headers=auth_header(manager_token)
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_manager_cannot_edit_draft(client: TestClient, user_token: str, manager_token: str) -> None:
    project = make_project(client, user_token)
    document_id = outline_document_id(client, user_token, project["outlineNodeId"])
    response = client.patch(
        f"/api/v1/documents/{document_id}/draft",
        headers=auth_header(manager_token),
        json={"body": "负责人不该能改"},
    )
    assert response.status_code == 403
