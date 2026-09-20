"""契约一致性：服务端实际暴露的接口必须与 docs/openapi.yaml 完全一致。

这个用例是「文档即合同」的强制执行点：任何一边多出或缺少 path+method 都会失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def _contract_operations() -> set[tuple[str, str]]:
    spec = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    operations: set[tuple[str, str]] = set()
    for route, item in spec["paths"].items():
        for method in item:
            if method.lower() in HTTP_METHODS:
                operations.add((f"/api/v1{route}", method.lower()))
    return operations


def _app_operations(client: TestClient) -> set[tuple[str, str]]:
    spec = client.app.openapi()  # type: ignore[attr-defined]
    operations: set[tuple[str, str]] = set()
    for route, item in spec["paths"].items():
        for method in item:
            if method.lower() in HTTP_METHODS:
                operations.add((route, method.lower()))
    return operations


def test_contract_file_is_openapi_31() -> None:
    spec = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert spec["openapi"] == "3.1.0"
    assert spec["paths"]


def test_every_contract_operation_is_implemented(client: TestClient) -> None:
    missing = _contract_operations() - _app_operations(client)
    assert not missing, f"契约里有、服务端没实现：{sorted(missing)}"


def test_no_undocumented_operation(client: TestClient) -> None:
    extra = _app_operations(client) - _contract_operations()
    assert not extra, f"服务端有、契约没写：{sorted(extra)}"


def test_operation_ids_are_unique(client: TestClient) -> None:
    spec = client.app.openapi()  # type: ignore[attr-defined]
    seen: dict[str, str] = {}
    for route, item in spec["paths"].items():
        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            op_id = operation.get("operationId")
            assert op_id, f"{method.upper()} {route} 缺少 operationId"
            assert op_id not in seen, f"operationId 重复：{op_id}"
            seen[op_id] = f"{method.upper()} {route}"


def test_error_response_shape_matches_contract(client: TestClient) -> None:
    response = client.get("/api/v1/projects")
    assert response.status_code == 401
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AUTH_REQUIRED"
    assert body["requestId"]
    assert body["timestamp"].endswith("Z")


@pytest.mark.parametrize("route", ["/api/v1/health", "/api/v1/meta"])
def test_public_routes_are_documented_as_public(client: TestClient, route: str) -> None:
    spec = client.app.openapi()  # type: ignore[attr-defined]
    operation = spec["paths"][route]["get"]
    assert operation.get("security") == []
