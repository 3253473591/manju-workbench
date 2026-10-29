"""契约探针自身的测试：探针必须"能抓错"，否则就是空转。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import contract_probe  # noqa: E402


def test_offline_probes_pass() -> None:
    results = contract_probe.run_offline()
    assert results, "探针没有产出任何检查"
    failed = [f"{item.channel}/{item.check}: {item.detail}" for item in results if not item.ok]
    assert not failed, "\n".join(failed)


def test_check_body_rejects_wrong_type() -> None:
    errors = contract_probe.check_body({"n": "1"}, {"n": int})
    assert errors and "期望" in errors[0]


def test_check_body_rejects_missing_key() -> None:
    errors = contract_probe.check_body({}, {"prompt": str})
    assert errors and "缺失" in errors[0]


def test_check_body_accepts_bool_as_not_int() -> None:
    """Python 里 bool 是 int 的子类，但 API 的 `n: true` 是错的。"""
    errors = contract_probe.check_body({"n": True}, {"n": int})
    assert errors, "bool 被当成了合法 int"
