"""real 模式护栏：没有可用凭证时必须失败，绝不静默退回 mock。

这是「未核验的档位不能伪装可用」的强制点（PRD §3、API对接文档 §8）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.conftest import accept, auth_header, wait_succeeded


@pytest.fixture
def real_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "APP_MODE", "real")
    # 确保没有任何凭证，模拟「尚未接入」的真实状态
    for name in (
        "ARK_DOUBAO_API_KEY",
        "ARK_SEEDREAM_API_KEY",
        "ARK_SEEDANCE_MINI_API_KEY",
    ):
        monkeypatch.setattr(settings, name, "")
    yield
    monkeypatch.setattr(settings, "APP_MODE", "mock")


def test_meta_reports_real_but_unavailable(
    client: TestClient, real_mode: None
) -> None:
    body = client.get("/api/v1/meta").json()
    assert body["mode"] == "real"
    # 没有任何档位通过真实调用核验
    assert body["realAvailable"] is False


def test_text_generation_fails_closed_without_credentials(
    client: TestClient, user_token: str, runner, real_mode: None
) -> None:
    token = user_token
    project = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": "real 护栏"}
    ).json()
    node = client.get(
        f"/api/v1/canvas/nodes/{project['outlineNodeId']}", headers=auth_header(token)
    ).json()
    document_id = node["document"]["document"]["id"]
    client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(token),
        json={"body": "大纲正文", "title": "大纲"},
    )

    submission_id = accept(
        client,
        client.post(
            f"/api/v1/documents/{document_id}/actions/optimize",
            headers={**auth_header(token), "Idempotency-Key": "real-guard-1"},
            json={},
        ),
    )
    runner()
    detail = client.get(
        f"/api/v1/submissions/{submission_id}", headers=auth_header(token)
    ).json()
    assert detail["submission"]["logicalStatus"] == "failed"
    assert detail["submission"]["errorCode"] == "PROVIDER_UNAVAILABLE"
    # 不产生任何业务正文，也不会有 mock 产物混进来
    assert detail["producedNodeIds"] == []


def test_estimate_reports_missing_credential(
    client: TestClient, admin_token: str, real_mode: None
) -> None:
    response = client.post(
        "/api/v1/estimate",
        headers=auth_header(admin_token),
        json={"operation": "image_draw", "count": 1, "productLabel": "lite"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["isMock"] is False
    # 单价已经登记，因此能给出确切预估；未核验的档位另加提示
    assert body["estimatedAmount"] == "0.22"
    assert body["unit"] == "元/张"


def test_estimate_text_uses_input_output_rates(
    client: TestClient, admin_token: str, real_mode: None
) -> None:
    """豆包输入 3 / 输出 15，两档单价不同，预估要把这一点带出来。"""
    response = client.post(
        "/api/v1/estimate",
        headers=auth_header(admin_token),
        json={"operation": "script_generate", "inputTokens": 1000, "outputTokens": 2000},
    )
    assert response.status_code == 200, response.text
    assert response.json()["estimatedAmount"] == "0.033"


def test_estimate_video_uses_token_rate(
    client: TestClient, admin_token: str, real_mode: None
) -> None:
    response = client.post(
        "/api/v1/estimate",
        headers=auth_header(admin_token),
        json={
            "operation": "video_draw",
            "productLabel": "mini",
            "durationSeconds": 5,
            "resolution": "720p",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["estimatedAmount"] == "2.49435"
    assert any("tokens/秒" in note for note in body["notes"])


def test_mock_and_real_are_distinguishable(
    client: TestClient, user_token: str, runner
) -> None:
    """mock 模式下 attempt 必须标 isMock，费用记 mock 而不是实际支出。"""
    token = user_token
    project = client.post(
        "/api/v1/projects", headers=auth_header(token), json={"name": "mock 标识"}
    ).json()
    node = client.get(
        f"/api/v1/canvas/nodes/{project['outlineNodeId']}", headers=auth_header(token)
    ).json()
    document_id = node["document"]["document"]["id"]
    client.post(
        f"/api/v1/documents/{document_id}/revisions",
        headers=auth_header(token),
        json={"body": "大纲正文", "title": "大纲"},
    )
    submission_id = accept(
        client,
        client.post(
            f"/api/v1/documents/{document_id}/actions/optimize",
            headers={**auth_header(token), "Idempotency-Key": "mock-flag-1"},
            json={},
        ),
    )
    detail = wait_succeeded(client, token, submission_id, runner)
    assert detail["attempts"]
    assert all(attempt["isMock"] is True for attempt in detail["attempts"])
