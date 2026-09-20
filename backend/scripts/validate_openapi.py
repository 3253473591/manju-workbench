"""校验 docs/openapi.yaml。

优先用 openapi-spec-validator 做完整校验；未安装时退化为内置结构 + $ref 解析检查，
保证 CI 与本地都不会因为缺依赖而静默跳过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
SPEC_PATH = BACKEND_DIR.parent / "docs" / "openapi.yaml"

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def _resolve_ref(doc: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ValueError(f"暂不支持外部 $ref: {ref}")
    node: Any = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise KeyError(ref)
        node = node[part]
    return node


def _walk_refs(node: Any, path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key == "$ref" and isinstance(value, str):
                found.append((here, value))
            else:
                found.extend(_walk_refs(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk_refs(value, f"{path}/{index}"))
    return found


def structural_check(doc: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    if doc.get("openapi") != "3.1.0":
        problems.append(f"openapi 版本应为 3.1.0，实际 {doc.get('openapi')!r}")
    for field in ("info", "paths", "components"):
        if field not in doc:
            problems.append(f"缺少顶层字段 {field}")
    for field in ("title", "version"):
        if field not in doc.get("info", {}):
            problems.append(f"info 缺少 {field}")

    paths = doc.get("paths", {})
    if not paths:
        problems.append("paths 为空")

    operation_ids: dict[str, str] = {}
    for route, item in paths.items():
        if not route.startswith("/"):
            problems.append(f"path 必须以 / 开头: {route}")
        if not isinstance(item, dict):
            problems.append(f"path item 必须是对象: {route}")
            continue
        operations = {k: v for k, v in item.items() if k in HTTP_METHODS}
        if not operations:
            problems.append(f"path 没有任何操作: {route}")
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                problems.append(f"{method.upper()} {route} 不是对象")
                continue
            op_id = operation.get("operationId")
            if not op_id:
                problems.append(f"{method.upper()} {route} 缺少 operationId")
            elif op_id in operation_ids:
                problems.append(
                    f"operationId 重复: {op_id}（{operation_ids[op_id]} 与 {method.upper()} {route}）"
                )
            else:
                operation_ids[op_id] = f"{method.upper()} {route}"
            responses = operation.get("responses")
            if not responses:
                problems.append(f"{method.upper()} {route} 缺少 responses")
            else:
                for status, response in responses.items():
                    if not (status.isdigit() or status == "default"):
                        problems.append(f"{method.upper()} {route} 非法状态码 {status}")
                    if isinstance(response, dict) and "$ref" not in response:
                        if "description" not in response:
                            problems.append(
                                f"{method.upper()} {route} 的 {status} 响应缺少 description"
                            )

    # $ref 全部可解析
    for where, ref in _walk_refs(doc):
        try:
            _resolve_ref(doc, ref)
        except (KeyError, ValueError) as exc:
            problems.append(f"$ref 无法解析: {where} -> {ref} ({exc})")

    # components.schemas 必须真的在 components 下
    for name, schema in doc.get("components", {}).get("schemas", {}).items():
        if not isinstance(schema, dict):
            problems.append(f"components.schemas.{name} 不是对象")

    return problems


def external_check(doc: dict[str, Any]) -> tuple[bool, str]:
    try:
        from openapi_spec_validator import validate  # type: ignore
    except ImportError:
        return False, "openapi-spec-validator 未安装，跳过完整校验"
    try:
        validate(doc)
    except Exception as exc:  # noqa: BLE001 - 校验器的异常类型不稳定
        return True, f"完整校验失败: {exc}"
    return True, "完整校验通过"


def main() -> int:
    raw = SPEC_PATH.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError:
        print("缺少 PyYAML，无法解析 YAML")
        return 2
    doc = yaml.safe_load(raw)

    problems = structural_check(doc)
    ran, message = external_check(doc)
    print(f"spec: {SPEC_PATH}")
    print(f"paths: {len(doc.get('paths', {}))}")
    print(f"schemas: {len(doc.get('components', {}).get('schemas', {}))}")
    print(f"external: {message}")

    if problems:
        print(f"结构问题 {len(problems)} 条：")
        for item in problems:
            print(f"  - {item}")
        return 1
    if ran and "失败" in message:
        return 1
    print("OpenAPI 校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
