"""对抗性探针 4：日期区间、diff、重复查询参数、HTTP 方法形态。

这几处是"看起来有校验、其实没接线"的高发区：
- `from` / `to` 的跨度上限在 `03` §6.2 写了 366 天，代码里有常量 —— 探针要确认它真的生效；
- diff 两端指向不存在/不相干的对象，不能 500，也不能悄悄给出无意义的 diff；
- 重复查询参数按"最后一次生效"定死（`03` §6.7），不能因为顺序不同而行为不同。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from manju.config import Settings
from tests.conftest import login


def _cost(client: TestClient, project_id: int, query: str):
    return client.get(f"/api/v1/projects/{project_id}/cost?{query}")


def test_cost_date_forms_never_500(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    for query in [
        "from=abc", "to=abc", "from=2026-13-01", "from=2026-02-30", "from=", "to=",
        "from=2026-10-19&to=2026-10-01", "from=2026-10-19T00:00:00",
    ]:
        response = _cost(client, world["project"].id, query)
        assert response.status_code < 500, f"{query} → {response.status_code} {response.text[:160]}"


def test_cost_rejects_oversized_range(client: TestClient, world: dict, settings: Settings) -> None:
    """`03` §6.2：跨度上限 366 天（防全表扫描）。常量存在但没接线的话这条会挂。"""
    login(client, world["owner"], settings)
    too_wide = _cost(client, world["project"].id, "from=2000-01-01&to=2026-12-31")
    assert too_wide.status_code == 422, too_wide.text
    assert too_wide.json()["error"]["code"] == "VALIDATION_RANGE"
    exactly = _cost(client, world["project"].id, "from=2025-10-19&to=2026-10-19")
    assert exactly.status_code == 200


def test_cost_group_by_edges(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    assert _cost(client, world["project"].id, "group_by=").status_code == 422
    assert _cost(client, world["project"].id, "group_by=stage").status_code == 422
    assert _cost(client, world["project"].id, "group_by=day").status_code == 200
    assert _cost(client, world["project"].id, "group_by=model&group_by=day").status_code == 200


def test_cost_by_plain_user_is_denied(client: TestClient, world: dict, settings: Settings) -> None:
    """成本看板是 lead/admin 的（`02` §2.2）；member 不该读到全项目花费。"""
    login(client, world["member"], settings)
    response = _cost(client, world["project"].id, "group_by=model")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERM_DENIED"


def test_duplicate_query_param_takes_last(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = f"/api/v1/assets?project_id={world['project'].id}"
    assert client.get(f"{url}&kind=image").status_code == 200
    assert client.get(f"{url}&kind=image&kind=nope").status_code == 422
    assert client.get(f"{url}&kind=nope&kind=image").status_code == 200


def test_asset_kind_is_case_sensitive(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    url = f"/api/v1/assets?project_id={world['project'].id}"
    assert client.get(f"{url}&kind=IMAGE").status_code == 422
    assert client.get(f"{url}&kind=video,video").status_code == 422


def test_revision_diff_edges(client: TestClient, session, world: dict, settings: Settings) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from manju.models import Revision

    now = datetime.now(UTC).replace(tzinfo=None)

    def revision(cost: str) -> Revision:
        row = Revision(
            target_kind="clip", target_id=world["clip"].id, prompt="p", cost=Decimal(cost),
            params_json={"res": "720p"}, created_at=now,
        )
        session.add(row)
        session.flush()
        return row

    left, right = revision("1.000000"), revision("2.500000")
    login(client, world["owner"], settings)
    assert client.get(f"/api/v1/revisions/{left.id}/diff/{left.id}").status_code == 200
    assert client.get(f"/api/v1/revisions/{left.id}/diff/{right.id}").status_code == 200
    assert client.get(f"/api/v1/revisions/{left.id}/diff/999999").status_code == 404
    assert client.get("/api/v1/revisions/999999/diff/1").status_code == 404


def test_http_method_and_path_shapes(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    clip_id = world["clip"].id
    assert client.put(f"/api/v1/clips/{clip_id}", json={}).status_code < 500
    assert client.head(f"/api/v1/clips/{clip_id}").status_code < 500
    assert client.options(f"/api/v1/clips/{clip_id}").status_code < 500
    assert client.get(f"/api/v1/clips/{clip_id}/").status_code < 500
    assert client.get("/api/v1/clips/%2F1").status_code < 500
    assert client.get(f"//api/v1/clips/{clip_id}").status_code < 500
