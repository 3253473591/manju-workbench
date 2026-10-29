"""`03` §5 ↔ `manju.constants` 的漂移检查。

`03` §12.1 要求这份比对进 CI —— 文档是给人读的、代码是给机器跑的两份数字，
不如每次跑测试都比一遍。方向只校验「文档 → 代码」：`03` §6.1 的字符串长度上限
按约定属于 pydantic schema，不在这里核对。
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from manju import constants

DOC = Path(__file__).resolve().parents[3] / "docs" / "开发文档" / "03-异常包容与边界值.md"
ROW = re.compile(r"^\|\s*`?([A-Z][A-Z0-9_]*)`?\s*\|(.+?)\|(.+?)\|\s*$")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# 文档一行对应多个常量的复合值
EXPANDED = {
    "JOB_HEARTBEAT": ("JOB_HEARTBEAT", "JOB_HEARTBEAT_DEAD"),
    "PAGE_LIMIT": ("PAGE_LIMIT_MIN", "PAGE_LIMIT_MAX", "PAGE_LIMIT_DEFAULT"),
    "LOGIN_FAIL_LIMIT": ("LOGIN_FAIL_LIMIT", "LOGIN_FAIL_WINDOW"),
}
# 文档写百分比、代码存比例
SCALED = {"JITTER": 100}


def doc_params() -> dict[str, str]:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("## 5. 参数总表")
    end = text.index("## 6. 边界值总表", start)
    params = {}
    for line in text[start:end].splitlines():
        match = ROW.match(line)
        if match:
            params[match.group(1)] = match.group(2).strip()
    return params


PARAMS = doc_params()


def test_doc_table_is_readable() -> None:
    assert len(PARAMS) >= 25, f"§5 只解析到 {len(PARAMS)} 行，表格格式可能变了"


@pytest.mark.parametrize("name", sorted(PARAMS))
def test_doc_param_has_constant(name: str) -> None:
    if name in constants.PROVIDER_CONFIGURED:
        assert not hasattr(constants, name), f"{name} 该由 provider 配置下发，不该有同名常量"
        return
    for attr in EXPANDED.get(name, (name,)):
        assert hasattr(constants, attr), f"03 §5 的 {name} 在 constants.py 里没有 {attr}"


@pytest.mark.parametrize("name", sorted(PARAMS))
def test_doc_value_matches_constant(name: str) -> None:
    if name in constants.PROVIDER_CONFIGURED:
        pytest.skip("provider 配置下发，无固定值")
    documented = [Decimal(value.replace(",", "")) for value in NUMBER.findall(PARAMS[name])]
    actual = []
    for attr in EXPANDED.get(name, (name,)):
        value = getattr(constants, attr)
        if isinstance(value, tuple):
            actual.extend(Decimal(str(item)) for item in value)
        else:
            actual.append(Decimal(str(value)))
    if name in SCALED:
        actual = [value * SCALED[name] for value in actual]
    assert documented[: len(actual)] == actual, f"{name}: 文档 {PARAMS[name]!r} vs 常量 {actual}"


def test_ref_capacity_is_not_a_domain_constant() -> None:
    """`01` §3.8：槽位容量归 provider 配置。出现名字就说明有人把它写死了。"""
    for attr in dir(constants):
        assert "CAPACITY" not in attr.upper(), f"{attr} 疑似写死了模型参考容量"


def test_idem_key_pattern_follows_max_length() -> None:
    pattern = re.compile(constants.IDEM_KEY_PATTERN)
    assert pattern.match("a" * constants.IDEM_KEY_MAX)
    assert not pattern.match("a" * (constants.IDEM_KEY_MAX + 1))
    assert not pattern.match("中文")


def test_numeric_invariants() -> None:
    assert constants.BUDGET_LIMIT_MIN < constants.BUDGET_JOB_DEFAULT <= constants.BUDGET_JOB_MAX
    assert constants.JOB_HEARTBEAT < constants.JOB_HEARTBEAT_DEAD < constants.JOB_TOTAL_TIMEOUT
    assert constants.LOCK_TTL <= constants.PRESENCE_TTL
    assert list(constants.RETRY_BACKOFF) == sorted(constants.RETRY_BACKOFF)
    assert constants.PAGE_LIMIT_MIN < constants.PAGE_LIMIT_DEFAULT <= constants.PAGE_LIMIT_MAX
