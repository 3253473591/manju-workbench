"""domain 层不变量（`01` §5.4）。纯函数：不碰 DB、不碰 HTTP。

返回问题列表而不是抛异常：同一个 clip 常常同时踩多条，一次报全比分三次 422 好用。
真正抛 `VALIDATION_INVARIANT` 的地方在 `clip_rules`（它负责把 DB 里的东西查出来喂进来）。

字段形态（长度、枚举、Literal）由 `schemas` 负责，这里只审**跨字段**关系，不重复校验形状。
"""

from __future__ import annotations

from decimal import Decimal

SHOT_SUM_TOLERANCE = Decimal("0.05")
MONEY_ULP = Decimal("0.000001")
KINDS = ("image", "video", "audio")

# `02` §6.2 的迁移图；同状态视为幂等。
# `approved → review` 是刻意的：审批绑定在**某一次产物**上（`current_revision_id`），
# 重跑产出新 take 后必须回到待审，否则新片会被上一次的"通过"直接背书。
CLIP_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"draft", "prompted"}),
    "prompted": frozenset({"prompted", "generating"}),
    "generating": frozenset({"generating", "review", "prompted"}),
    "review": frozenset({"review", "approved", "rejected"}),
    "rejected": frozenset({"rejected", "generating"}),
    # `approved → rejected` 是刻意的：通过之后又发现问题时，人需要"撤销通过并打回"一步到位，
    # 否则得先 approved → review 再 review → rejected 两次点击，中间还会闪一下待审状态。
    "approved": frozenset({"approved", "review", "rejected"}),
}


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def _missing(items: list[dict], required: tuple[str, ...]) -> list[str]:
    """JSON 列的脏数据防线（`01` §3 / `03` §2）：历史行/种子导入可能缺键。

    缺键就回一条 problem，让上层给 422 —— `KeyError` 冒到 API 就是 500，把"数据脏"报成"服务坏了"。
    """
    bad = sorted({key for item in items for key in required if item.get(key) is None})
    return [f"JSON 脏数据：缺 {','.join(bad)}，该行需要重建"] if bad else []


def shots_problems(shots: list[dict] | None, duration_s: object, require_shots: bool = True) -> list[str]:
    """`require_shots=False` 用于 draft 阶段：允许先建片段、后补分镜，但已填的镜头照样要闭合。"""
    if not shots:
        return ["shots 不能为空：至少 1 个镜头才能生成（`03` §6.3）"] if require_shots else []
    problems = _missing(shots, ("no", "start", "end"))
    if problems:
        return problems
    numbers = [shot["no"] for shot in shots]
    if numbers != list(range(1, len(shots) + 1)):
        problems.append(f"镜头号必须从 1 连续：实际 {numbers}")
    if _dec(shots[0]["start"]) != 0:
        problems.append(f"首镜 start 必须为 0，实际 {shots[0]['start']}")
    for left, right in zip(shots, shots[1:]):
        if _dec(left["end"]) != _dec(right["start"]):
            problems.append(f"镜头{left['no']} end={left['end']} 与镜头{right['no']} start={right['start']} 不衔接")
    for shot in shots:
        if _dec(shot["end"]) <= _dec(shot["start"]):
            problems.append(f"镜头{shot['no']} 的 end 必须大于 start")
    total = _dec(shots[-1]["end"])
    if abs(total - _dec(duration_s)) > SHOT_SUM_TOLERANCE:
        problems.append(f"末镜 end={total}s 与片段时长 {duration_s}s 相差超过 {SHOT_SUM_TOLERANCE}s")
    return problems


def slots_problems(slots: list[dict] | None, capacity: dict[str, int] | None = None) -> list[str]:
    """`idx` 按 kind 各自从 1 连续；每个槽位至少要有 `asset_id` 或 `asset_ref`。

    数量上限来自调用模型的容量（`01` §3.8），由 provider 配置提供 —— 传 `None` 表示
    "当前没有模型上下文"（例如 clip 还没到生成阶段），此时只审结构、不审容量。
    """
    if not slots:
        return []
    problems = _missing(slots, ("idx", "kind"))
    if problems:
        return problems
    for kind in KINDS:
        idxs = sorted(slot["idx"] for slot in slots if slot["kind"] == kind)
        if idxs and idxs != list(range(1, len(idxs) + 1)):
            problems.append(f"{kind} 槽位序号必须从 1 连续：实际 {idxs}")
        if capacity is not None and len(idxs) > capacity.get(kind, 0):
            problems.append(f"{kind} 参考 {len(idxs)} 个，超过模型容量 {capacity.get(kind, 0)}")
    for slot in slots:
        if slot.get("asset_id") is None and not slot.get("asset_ref"):
            problems.append(f"{slot['kind']} 槽位 {slot['idx']} 既没有 asset_id 也没有 asset_ref")
    return problems


def dialogue_problems(dialogue: list[dict] | None, shots: list[dict] | None, prompt: str | None) -> list[str]:
    """台词要指向存在的镜头，且**逐字出现在提示词正文里**（`经验教训.md` #94）。

    prompt 为空时跳过内嵌检查：那是"还没写提示词"的中间态，内嵌是生成前的门槛，
    不拦保存（`01` §5.4 的校验点写的是"生成前"）。
    """
    if not dialogue:
        return []
    problems = _missing(dialogue, ("text",))
    if problems:
        return problems
    numbers = {shot["no"] for shot in (shots or [])}
    for line in dialogue:
        shot_no = line.get("shot_no")
        if shot_no is not None and shot_no not in numbers:
            problems.append(f"台词「{line['text'][:12]}…」指向不存在的镜头 {shot_no}")
        if prompt and line["text"] not in prompt:
            problems.append(f"台词「{line['text'][:12]}…」没有内嵌进提示词正文")
    return problems


def acceptance_problems(acceptance: list[dict] | None) -> list[str]:
    if not acceptance:
        return []
    problems = _missing(acceptance, ("id", "criterion"))
    if problems:
        return problems
    ids = [item["id"] for item in acceptance]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        problems.append(f"验收点 id 重复：{duplicates}")
    known = set(ids)
    for item in acceptance:
        target = item.get("superseded_by")
        if target is not None and target not in known:
            problems.append(f"验收点 {item['id']} 的 superseded_by={target} 指向不存在的 id")
    return problems


def cost_problems(
    cost: object,
    unit_price: object | None = None,
    video_seconds: object | None = None,
    tokens: object | None = None,
) -> list[str]:
    """`cost == unit_price × 数量`。

    `unit_price` 只存 6 位小数，所以等式只能到"半个最小单位"：容差 = 数量 × 0.5e-6。
    真实样本 12 s / 2.27 元：unit_price=0.189167，乘回去 2.270004，差 4e-6 < 6e-6，通过。
    TODO: token 计价的 `unit_price` 单位（元/千 token？）需在 provider 配置里定死后再收紧这条。
    """
    if unit_price is None or cost is None:
        return []
    quantity = video_seconds if video_seconds is not None else tokens
    if quantity is None:
        return []
    amount = _dec(quantity)
    expected = _dec(unit_price) * amount
    tolerance = abs(amount) * MONEY_ULP / 2
    if abs(_dec(cost) - expected) > tolerance:
        return [f"cost={cost} 与 unit_price×数量={expected} 不一致（容差 {tolerance}）"]
    return []


def transition_problems(current: str, target: str) -> list[str]:
    allowed = CLIP_TRANSITIONS.get(current)
    if allowed is None:
        return [f"未知的 clip 状态：{current}"]
    if target not in allowed:
        return [f"clip 不能从 {current} 迁移到 {target}（合法目标：{sorted(allowed)}）"]
    return []
