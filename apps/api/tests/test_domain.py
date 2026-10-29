"""domain 六条不变量的单元测试（`01` §5.4）。纯函数，不连库。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from manju import domain

GOOD_SHOTS = [
    {"no": 1, "start": "0", "end": "3.50", "desc": "远景急推"},
    {"no": 2, "start": "3.50", "end": "12.00", "desc": "贴脸特写"},
]


def joined(problems: list[str]) -> str:
    return " | ".join(problems)


# ---------------------------------------------------------------- 1. 分镜闭合


def test_shots_ok() -> None:
    assert domain.shots_problems(GOOD_SHOTS, "12.00") == []


def test_shots_must_start_at_zero() -> None:
    shots = [{"no": 1, "start": "0.50", "end": "12.00", "desc": "a"}]
    assert "首镜 start 必须为 0" in joined(domain.shots_problems(shots, "12.00"))


def test_shots_must_be_contiguous() -> None:
    shots = [
        {"no": 1, "start": "0", "end": "3.50", "desc": "a"},
        {"no": 2, "start": "4.00", "end": "12.00", "desc": "b"},
    ]
    assert "不衔接" in joined(domain.shots_problems(shots, "12.00"))


def test_shots_tolerance_is_half_a_tick() -> None:
    shots = [{"no": 1, "start": "0", "end": "12.04", "desc": "a"}]
    assert domain.shots_problems(shots, "12.00") == []
    far = [{"no": 1, "start": "0", "end": "12.06", "desc": "a"}]
    assert "相差超过" in joined(domain.shots_problems(far, "12.00"))


def test_shots_numbering_from_one() -> None:
    shots = [{"no": 2, "start": "0", "end": "12.00", "desc": "a"}]
    assert "从 1 连续" in joined(domain.shots_problems(shots, "12.00"))


def test_empty_shots_allowed_in_draft_only() -> None:
    assert domain.shots_problems(None, "12.00", require_shots=False) == []
    assert "shots 不能为空" in joined(domain.shots_problems(None, "12.00"))


# ---------------------------------------------------------------- 2. 参考槽位


def test_slots_contiguous_per_kind() -> None:
    slots = [
        {"idx": 1, "kind": "image", "asset_id": 1},
        {"idx": 2, "kind": "image", "asset_id": 2},
        {"idx": 1, "kind": "video", "asset_id": 3},
    ]
    assert domain.slots_problems(slots) == []


def test_slots_gap_detected() -> None:
    slots = [{"idx": 1, "kind": "image", "asset_id": 1}, {"idx": 3, "kind": "image", "asset_id": 3}]
    assert "必须从 1 连续" in joined(domain.slots_problems(slots))


def test_slots_capacity_per_model() -> None:
    slots = [{"idx": i, "kind": "image", "asset_id": i} for i in range(1, 5)]
    assert domain.slots_problems(slots, {"image": 9, "video": 3, "audio": 3}) == []
    assert "超过模型容量" in joined(domain.slots_problems(slots, {"image": 3, "video": 3, "audio": 3}))


def test_slot_needs_asset_id_or_ref() -> None:
    assert "既没有 asset_id 也没有 asset_ref" in joined(domain.slots_problems([{"idx": 1, "kind": "image"}]))


# ---------------------------------------------------------------- 3. 台词


def test_dialogue_must_be_embedded() -> None:
    dialogue = [{"speaker": "system", "text": "退钱", "shot_no": 2}]
    prompt = "镜头2（3.5-12秒）：她吼「退钱」"
    assert domain.dialogue_problems(dialogue, GOOD_SHOTS, prompt) == []
    assert "没有内嵌" in joined(domain.dialogue_problems(dialogue, GOOD_SHOTS, "镜头2：她吼"))


def test_dialogue_embedding_skipped_without_prompt() -> None:
    dialogue = [{"speaker": "system", "text": "退钱", "shot_no": 2}]
    assert domain.dialogue_problems(dialogue, GOOD_SHOTS, None) == []


def test_dialogue_shot_must_exist() -> None:
    dialogue = [{"speaker": "system", "text": "退钱", "shot_no": 9}]
    assert "不存在的镜头 9" in joined(domain.dialogue_problems(dialogue, GOOD_SHOTS, "退钱"))


# ---------------------------------------------------------------- 4. 验收点


def test_acceptance_ids_unique() -> None:
    items = [{"id": "a1", "criterion": "x"}, {"id": "a1", "criterion": "y"}]
    assert "id 重复" in joined(domain.acceptance_problems(items))


def test_acceptance_superseded_must_exist() -> None:
    items = [{"id": "a1", "criterion": "x", "superseded_by": "a9"}]
    assert "指向不存在的 id" in joined(domain.acceptance_problems(items))
    ok = [{"id": "a1", "criterion": "x"}, {"id": "a2", "criterion": "y", "superseded_by": "a1"}]
    assert domain.acceptance_problems(ok) == []


# ---------------------------------------------------------------- 5. 成本口径


def test_cost_matches_with_unit_price_ulp() -> None:
    # 真实样本：12 s / 2.27 元，unit_price 反算 0.189167，乘回去 2.270004
    assert domain.cost_problems("2.27", "0.189167", "12", None) == []


def test_cost_mismatch_detected() -> None:
    assert "不一致" in joined(domain.cost_problems("2.50", "0.189167", "12", None))


def test_cost_needs_unit_price_and_quantity() -> None:
    assert domain.cost_problems("2.27", None, "12", None) == []
    assert domain.cost_problems("2.27", "0.189167", None, None) == []


def test_cost_token_branch() -> None:
    # token 计价单位还没定（见 domain.cost_problems 的 TODO），这里只钉住"能用"
    assert domain.cost_problems("1.50", "0.0000003", None, Decimal("5000000")) == []


# ---------------------------------------------------------------- 6. 状态机


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "prompted"),
        ("prompted", "generating"),
        ("generating", "review"),
        ("generating", "prompted"),
        ("review", "approved"),
        ("review", "rejected"),
        ("rejected", "generating"),
        ("approved", "approved"),
    ],
)
def test_legal_clip_transitions(current: str, target: str) -> None:
    assert domain.transition_problems(current, target) == []


@pytest.mark.parametrize(
    ("current", "target"),
    [("draft", "approved"), ("approved", "draft"), ("rejected", "approved"), ("draft", "review")],
)
def test_illegal_clip_transitions(current: str, target: str) -> None:
    assert "不能从" in joined(domain.transition_problems(current, target))


def test_unknown_status() -> None:
    assert "未知的 clip 状态" in joined(domain.transition_problems("bogus", "draft"))
