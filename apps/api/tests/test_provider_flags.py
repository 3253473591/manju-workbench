"""/admin/providers 的写操作：运行时开关（多副本共享，落在 `runtime_flag`）。

为什么值得单独测：
- 停用必须**在花钱之前**生效（steps / 出图 / submit 三个入口）；
- 状态要能跨会话看到（不是某个进程里的字典）；
- 写操作必须先登录且**只能是 admin**（owner 也不行 —— 这是全局配置域）；
- 停用必须写原因（"为什么停"是给下一个值班的人看的）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju.config import Settings
from manju.models import RuntimeFlag
from tests.conftest import login


def test_admin_can_disable_and_enable_text_channel(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    login(client, world["admin"], settings)
    closed = client.post(
        "/api/v1/admin/providers/text/state", json={"enabled": False, "reason": "账单止损"}
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["text"]["enabled"] is False
    assert closed.json()["text"]["reason"] == "账单止损"

    row = session.query(RuntimeFlag).filter_by(kind="provider", key_name="text").one()
    assert row.enabled == 0 and row.actor_id == world["admin"].id

    opened = client.post("/api/v1/admin/providers/text/state", json={"enabled": True})
    assert opened.status_code == 200
    assert opened.json()["text"]["enabled"] is True
    assert opened.json()["text"]["reason"] is None  # 启用时清掉原因


def test_disabled_text_blocks_step_before_spending(
    client: TestClient, world: dict, settings: Settings
) -> None:
    login(client, world["admin"], settings)
    client.post("/api/v1/admin/providers/text/state", json={"enabled": False, "reason": "演练"})

    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/projects/{world['project'].id}/steps/S1/run")
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert error["detail"]["channel"] == "text"
    assert error["detail"]["reason"] == "演练"


def test_disabled_video_blocks_generate_before_creating_job(
    client: TestClient, session: Session, world: dict, settings: Settings
) -> None:
    from manju.models import Job

    login(client, world["admin"], settings)
    client.post("/api/v1/admin/providers/video/state", json={"enabled": False, "reason": "额度用完"})

    login(client, world["owner"], settings)
    response = client.post(
        f"/api/v1/clips/{world['clip'].id}/generate?mode=auto",
        json={"res": "draft"},
        headers={"Idempotency-Key": "flag-video", "If-Match": f'"{world["clip"].version}"'},
    )
    assert response.status_code == 503
    assert response.json()["error"]["detail"]["channel"] == "video"
    assert session.query(Job).count() == 0  # 闸在"建 job"之前


def test_disabled_image_channel_blocks_generation(
    client: TestClient, world: dict, settings: Settings
) -> None:
    login(client, world["admin"], settings)
    client.post("/api/v1/admin/providers/image/state", json={"enabled": False, "reason": "换 key"})
    login(client, world["owner"], settings)
    response = client.post(f"/api/v1/projects/{world['project'].id}/images", json={"prompt": "p"})
    assert response.status_code == 503
    assert response.json()["error"]["detail"]["channel"] == "image"


def test_only_admin_can_toggle_and_reason_is_required(
    client: TestClient, world: dict, settings: Settings
) -> None:
    login(client, world["owner"], settings)  # lead 是项目 owner，不是平台 admin
    assert client.post("/api/v1/admin/providers/text/state", json={"enabled": False, "reason": "x"}).status_code == 403

    login(client, world["admin"], settings)
    assert client.post("/api/v1/admin/providers/text/state", json={"enabled": False}).status_code == 422
    assert client.post("/api/v1/admin/providers/nope/state", json={"enabled": False, "reason": "x"}).status_code == 422


def test_flag_is_visible_across_requests_and_survives_login_switch(
    client: TestClient, world: dict, settings: Settings
) -> None:
    """状态在 DB 里，不是某个进程的字典 —— 换一个会话登录照样看得到。"""
    login(client, world["admin"], settings)
    client.post("/api/v1/admin/providers/vision/state", json={"enabled": False, "reason": "视觉判错"})
    client.post("/api/v1/auth/logout")
    login(client, world["admin"], settings)
    status = client.get("/api/v1/admin/providers").json()
    assert status["vision"]["enabled"] is False
    assert status["vision"]["reason"] == "视觉判错"
    assert status["text"]["enabled"] is True  # 其他通道不受影响
