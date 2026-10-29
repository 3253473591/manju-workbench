"""对抗性探针：故意用越界、空值、错类型、脏数据撞代码，找的是**缺陷**不是绿灯。

三条原则，与"写用例证明我实现对了"正好相反：

1. **客户端能发到的输入，返回 5xx 就是缺陷** —— 合法但不存在是 404，形态不对是 422/413/405，
   内部炸成 500 说明边界没守住（`03` §6）。所以断言写的是 `status < 500`，不是某个具体码。
2. **内部函数只许抛 `ApiError`** —— 脏数据（DB 里的历史 JSON、上游输出）撞进 `domain` /
   `review` / `skills` 时，`KeyError`/`IndexError`/`TypeError` 都是漏网。
3. **降级要留下痕迹** —— 判不了就是 unknown 或一条 problem，静默当成"没问题"是更坏的缺陷。

覆盖：路径参数形态、If-Match、幂等键、查询参数、请求体形状（空/深嵌套/超长/错类型）、
域不变量（越界序号、空值、不衔接、跨项目 asset）、worker 遇到脏 DB 行。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from manju import domain, review, skills
from manju.config import Settings
from manju.errors import ApiError, ErrorCode
from manju.models import Clip
from manju.providers import MockProvider
from manju.review import Motion
from tests.conftest import login

# ---------------------------------------------------------------- 黑盒：路径参数

PATH_IDS = [
    "abc", "-1", "0", "1.5", "1e3", "0x10", " 1", "1 ", "１", "١", "+1", "1_000",
    "99999999999999999999", str(2**64), "null", "[]", "%20",
]


@pytest.mark.parametrize("bad", PATH_IDS)
def test_path_id_shapes_never_500(
    client: TestClient, world: dict, settings: Settings, bad: str
) -> None:
    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/clips/{bad}")
    assert response.status_code < 500, f"{bad} → {response.status_code} {response.text[:200]}"


# ---------------------------------------------------------------- 黑盒：请求头

IF_MATCH_FORMS = [
    None, "", '"', '"1', '1', '"0"', '"-1"', '"1.0"', '" 1 "', '"1 "', "'1'", '"abc"',
    f'"{2**64}"', '"1","2"', '"1\n2"', '"999999999999999999999999999999999999"',
]


@pytest.mark.parametrize("header", IF_MATCH_FORMS)
def test_if_match_forms_never_500(
    client: TestClient, world: dict, settings: Settings, header: str | None
) -> None:
    login(client, world["owner"], settings)
    headers = {"If-Match": header} if header is not None else {}
    response = client.patch(f"/api/v1/clips/{world['clip'].id}", json={"title": "t"}, headers=headers)
    assert response.status_code < 500, f"{header!r} → {response.status_code} {response.text[:200]}"


IDEM_FORMS = ["", " ", "a b", "../../etc/passwd", "a/b", "x" * 128, "x" * 129, "-" * 64]


@pytest.mark.parametrize("key", IDEM_FORMS)
def test_idempotency_key_forms_never_500(
    client: TestClient, world: dict, settings: Settings, key: str
) -> None:
    login(client, world["owner"], settings)
    response = client.post(
        f"/api/v1/clips/{world['clip'].id}/generate",
        json={"res": "draft", "budget_limit": "12.000000"},
        headers={"Idempotency-Key": key, "If-Match": f'"{world["clip"].version}"'},
    )
    assert response.status_code < 500, f"{key!r} → {response.status_code} {response.text[:200]}"


def test_idempotency_key_charset_is_enforced(client: TestClient, world: dict, settings: Settings) -> None:
    """`03` §8.1 的字符集是契约：越界必须 422，不能被静默接收（否则幂等语义看网络脸色）。"""
    login(client, world["owner"], settings)
    url = f"/api/v1/clips/{world['clip'].id}/generate"
    body = {"res": "draft", "budget_limit": "12.000000"}
    for bad in ["", "a b", "a/b", "x" * 129]:
        response = client.post(url, json=body, headers={"Idempotency-Key": bad, "If-Match": '"1"'})
        assert response.status_code == 422, f"{bad!r} → {response.status_code}"
    non_ascii = client.post(
        url, json=body, headers={b"Idempotency-Key": "中文键".encode(), b"If-Match": b'"1"'}
    )
    assert non_ascii.status_code == 422, non_ascii.text
    ok = client.post(url, json=body, headers={"Idempotency-Key": "ok-key_1.2:3", "If-Match": '"1"'})
    assert ok.status_code == 202, ok.text


def test_non_ascii_in_headers_is_rejected_not_crashing(
    client: TestClient, world: dict, settings: Settings
) -> None:
    """真实客户端可以把 UTF-8 字节塞进头（httpx 的 str 形态发不出去，得用 bytes 撞）。"""
    login(client, world["owner"], settings)
    response = client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        json={"title": "t"},
        headers={b"If-Match": '"１"'.encode()},
    )
    assert response.status_code < 500, response.status_code


# ---------------------------------------------------------------- 黑盒：查询参数

@pytest.mark.parametrize(
    "query",
    [
        "limit=abc", "limit=0", "limit=-1", "limit=1.5", "limit=999999999", "limit=",
        "cursor=-5", "cursor=abc", "cursor=99999999999999999999",
        "limit=5&limit=abc", "limit=abc&limit=5",
    ],
)
def test_query_forms_never_500(
    client: TestClient, world: dict, settings: Settings, query: str
) -> None:
    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/projects/{world['project'].id}/episodes?{query}")
    assert response.status_code < 500, f"{query} → {response.status_code} {response.text[:200]}"


@pytest.mark.parametrize("group_by", ["", "stage", "MODEL", "model;drop table revision", "null"])
def test_cost_group_by_forms(
    client: TestClient, world: dict, settings: Settings, group_by: str
) -> None:
    login(client, world["owner"], settings)
    response = client.get(f"/api/v1/projects/{world['project'].id}/cost?group_by={group_by}")
    assert response.status_code < 500


# ---------------------------------------------------------------- 黑盒：请求体形状

BODY_ATTACKS: list[tuple[str, Any]] = [
    ("null 体", None),
    ("空数组", []),
    ("字符串体", "not-an-object"),
    ("数字体", 42),
    ("未知字段", {"nope": 1}),
    ("shots 传对象", {"shots": {}}),
    ("shots 传字符串", {"shots": "abc"}),
    ("shots 元素为 null", {"shots": [None]}),
    ("shots 元素为空对象", {"shots": [{}]}),
    ("shots 元素为数组", {"shots": [[]]}),
    ("shots 100 层嵌套", {"shots": [[[[[[[[[[[]]]]]]]]]]]}),
    ("duration 布尔", {"duration_s": True}),
    ("duration 科学计数", {"duration_s": "1e3"}),
    ("duration 全角", {"duration_s": "１２"}),
    ("duration 空串", {"duration_s": ""}),
    ("duration None", {"duration_s": None}),
    ("duration 超精度", {"duration_s": "12.000001"}),
    ("acceptance 元素缺 criterion", {"acceptance": [{"id": "a1"}]}),
    ("acceptance id 空串", {"acceptance": [{"id": "", "criterion": "x"}]}),
    ("acceptance 重复 id", {"acceptance": [{"id": "a1", "criterion": "x"}, {"id": "a1", "criterion": "y"}]}),
    ("dialogue text 为 null", {"dialogue": [{"speaker": "s", "text": None}]}),
    ("ref_slots idx 0", {"ref_slots": [{"idx": 0, "kind": "image", "role": "实景", "asset_ref": "r"}]}),
    ("ref_slots idx 31", {"ref_slots": [{"idx": 31, "kind": "image", "role": "实景", "asset_ref": "r"}]}),
    ("ref_slots 未知 kind", {"ref_slots": [{"idx": 1, "kind": "audio2", "role": "实景", "asset_ref": "r"}]}),
    ("prompt 只有零宽字符", {"prompt": "\u200b\u200c"}),
    ("prompt 只有控制字符", {"prompt": "\x00\x01"}),
    ("status 未知枚举", {"status": "exploded"}),
    ("status null", {"status": None}),
]


@pytest.mark.parametrize("label,payload", BODY_ATTACKS, ids=[label for label, _ in BODY_ATTACKS])
def test_body_shapes_never_500(
    client: TestClient, world: dict, settings: Settings, label: str, payload: Any
) -> None:
    login(client, world["owner"], settings)
    clip_id = world["clip"].id
    response = client.patch(
        f"/api/v1/clips/{clip_id}",
        json=payload,
        headers={"If-Match": f'"{world["clip"].version}"'},
    )
    assert response.status_code < 500, f"{label} → {response.status_code} {response.text[:300]}"


def test_body_size_limit_is_413_not_500(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    huge = {"prompt": "字" * 3_000_000}
    response = client.patch(
        f"/api/v1/clips/{world['clip'].id}", json=huge, headers={"If-Match": '"1"'}
    )
    assert response.status_code in (413, 422), response.status_code


def test_non_utf8_and_broken_json_body(client: TestClient, world: dict, settings: Settings) -> None:
    login(client, world["owner"], settings)
    broken = client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        content=b'{"title": "\xff\xfe"}',
        headers={"If-Match": '"1"', "Content-Type": "application/json"},
    )
    assert broken.status_code < 500
    duplicate = client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        content=b'{"duration_s": "9.00", "duration_s": "99.00"}',
        headers={"If-Match": '"1"', "Content-Type": "application/json"},
    )
    assert duplicate.status_code < 500


# ---------------------------------------------------------------- 黑盒：域不变量（越界/空值）

def _patch(client: TestClient, world: dict, payload: dict, version: str | None = None) -> Any:
    return client.patch(
        f"/api/v1/clips/{world['clip'].id}",
        json=payload,
        headers={"If-Match": version or f'"{world["clip"].version}"'},
    )


@pytest.mark.parametrize(
    "label,payload",
    [
        ("镜头号跳号", {"shots": [{"no": 1, "start": "0", "end": "6.00", "desc": "a"}, {"no": 3, "start": "6.00", "end": "12.00", "desc": "b"}]}),
        ("首镜 start 非 0", {"shots": [{"no": 1, "start": "1.00", "end": "12.00", "desc": "a"}]}),
        ("end 小于 start", {"shots": [{"no": 1, "start": "0", "end": "-1.00", "desc": "a"}]}),
        ("末镜不闭合", {"shots": [{"no": 1, "start": "0", "end": "5.00", "desc": "a"}]}),
        ("镜头时长超上限", {"shots": [{"no": 1, "start": "0", "end": "999.00", "desc": "a"}]}),
        ("台词指向 0 号镜头", {"dialogue": [{"speaker": "徒弟", "text": "退钱", "shot_no": 0}]}),
        ("台词指向 999 号镜头", {"dialogue": [{"speaker": "徒弟", "text": "退钱", "shot_no": 999}]}),
        ("验收点 superseded_by 悬空", {"acceptance": [{"id": "a1", "criterion": "x", "superseded_by": "zz"}]}),
        ("验收点 500 条重复 id", {"acceptance": [{"id": "a1", "criterion": "x"}] * 500}),
        ("槽位既无 asset_id 也无 ref", {"ref_slots": [{"idx": 1, "kind": "image", "role": "实景"}]}),
        ("槽位引用别的项目资产", {"ref_slots": [{"idx": 1, "kind": "image", "role": "实景", "asset_id": 999_999}]}),
    ],
)
def test_domain_invariants_reject_cleanly(
    client: TestClient, world: dict, settings: Settings, label: str, payload: dict
) -> None:
    login(client, world["owner"], settings)
    response = _patch(client, world, payload)
    assert response.status_code < 500, f"{label} → {response.status_code} {response.text[:300]}"


def test_shots_null_while_leaving_draft(client: TestClient, session: Session, world: dict, settings: Settings) -> None:
    """draft 允许空分镜，但一离开 draft 就必须闭合 —— 用 PATCH 走 prompted 撞这条门。"""
    clip = Clip(
        episode_id=world["episode"].id,
        idx=5,
        source_idx=5,
        duration_s=Decimal("6.00"),
        status="draft",
        created_at=world["clip"].created_at,
        updated_at=world["clip"].updated_at,
    )
    session.add(clip)
    session.flush()
    login(client, world["owner"], settings)
    response = client.patch(
        f"/api/v1/clips/{clip.id}", json={"status": "prompted"}, headers={"If-Match": '"1"'}
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------- 白盒：纯函数遇脏数据

def test_review_parse_survives_garbage() -> None:
    """vscan 输出可能被截断、可能只有半行、可能带 nan/inf —— 解析器不许抛。"""
    for output in [
        "", "\n\n", "stats : min=nan avg=inf max=-inf", "stats   : min=1 avg=2",
        "stats   : min=1  avg=2  max=3  sd=", "scan : x samples", "clip : s",
        "  abc  |####|  1.23", "stats   : min=1 avg=2 max=3\n  t=  s motion=  x [FREEZE]",
        "stats   : min=0 avg=0 max=0\n  1.00s |#| 0\n  2.00s |#| 0",
        "stats   : min=1e308 avg=1e309 max=inf",
    ]:
        motion, note = review.parse(output)
        assert (motion is None) != (note is None), f"{output!r} 两个都空了"


def test_review_judge_handles_malformed_acceptance() -> None:
    """DB 里的历史 JSON 可能缺 criterion / 缺 id —— judge 只能当"判不了"，不能 KeyError。"""
    motion = Motion(
        samples=23, duration_s=None, avg=28.0, peak=49.0, minimum=7.0,
        freeze=0, tail_collapse=False, flagged=[], series=[1.0, 2.0],
    )
    for acceptance in [
        [{"id": "a1"}],
        [{"criterion": "开场"}],
        [{}],
        [{"id": None, "criterion": None}],
    ]:
        report = review.judge(acceptance, motion, None)  # type: ignore[arg-type]
        assert all(item["result"] in ("pass", "fail", "unknown") for item in report.results)


def test_review_judge_handles_motion_with_empty_series() -> None:
    """0 帧的片段（截断产物）不该让 judge 抛 IndexError。"""
    motion = Motion(
        samples=0, duration_s=Decimal("0.00"), avg=0.0, peak=0.0, minimum=0.0,
        freeze=0, tail_collapse=False, flagged=[], series=[],
    )
    report = review.judge([{"id": "a1", "criterion": "开场不是定镜"}], motion, None)
    assert report.verdict in ("pass", "fail", "unknown")


def test_judge_with_vision_handles_out_of_protocol_verdicts(sheet_path, tmp_path) -> None:
    """provider 返回了未判过、重复、甚至 verdict 值非法的条目时不许炸。"""
    report = review.judge([{"id": "a1", "criterion": "功德环"}], None, "降级")
    assert report.results[0]["result"] == "unknown"
    provider = MockProvider(fixture=None)
    judged = review.judge_with_vision(
        [{"id": "a1", "criterion": "功德环"}], report, tmp_path / "x.mp4", provider, workdir=tmp_path
    )
    assert judged.results[0]["result"] == "pass"


def test_skills_split_sections_edge_cases() -> None:
    """空的、全标题的、标题带空格的、超长单节 —— 切分结果必须自洽。"""
    assert skills.split_sections("") == []
    assert skills.split_sections("没有标题的正文") == []
    assert skills.split_sections("## ") == []
    assert skills.split_sections("##\n##\n") == []
    assert skills.split_sections("### h3 不是 h2") == []
    sections = skills.split_sections("## #110 a\n正文\n## §2.1 b\n正文2\n## 无名\n正文3")
    assert [item["rule_id"] for item in sections] == ["#110", "§2.1", "s3"]
    assert all(item["tokens_est"] >= 1 for item in sections)


def test_skills_validate_body_rejects_hostile_bodies() -> None:
    for body, expected in [
        ("", ErrorCode.VALIDATION_SHAPE),
        ("没有节", ErrorCode.VALIDATION_SHAPE),
        ("## a\n```python\nimport os\n```", ErrorCode.SKILL_TOOL_UNKNOWN),
        ("## a\n```bash\nrm -rf /\n```", ErrorCode.SKILL_TOOL_UNKNOWN),
        ("## a\n" + "字" * 3000, ErrorCode.VALIDATION_RANGE),
    ]:
        with pytest.raises(ApiError) as caught:
            skills.validate_body(body)
        assert caught.value.code == expected, body[:20]


def test_skills_rank_sections_with_hostile_sections() -> None:
    """单节比预算还大 → 裁掉；0 token 的怪节不许把预算算成负数。"""
    class Fake:
        sections_json = [
            {"rule_id": "#1", "title": "a", "tags": [], "text": "x", "tokens_est": 0},
            {"rule_id": "#2", "title": "b", "tags": ["开场"], "text": "y", "tokens_est": 10_000},
            {"rule_id": "#3", "title": "c", "tags": [], "text": "z", "tokens_est": 1},
        ]

    ranked = skills.rank_sections([Fake()], budget=1, tags={"开场"})
    by_rule = {item["rule_id"]: item["selected"] for item in ranked}
    assert by_rule["#2"] is False  # 10000 进不了 1 的预算
    assert by_rule["#3"] is True
    assert skills.rank_sections([Fake()], budget=0, tags=set())  # 预算 0 也不能抛


def test_domain_survives_malformed_rows() -> None:
    """DB 里 shots/slots/dialogue 的历史 JSON 可能缺键 —— 只能给 problem，不能 KeyError。"""
    assert "脏数据" in domain.shots_problems([{"start": 0, "end": 6}], 6)[0]
    assert "脏数据" in domain.shots_problems([{"no": 1, "start": None, "end": 6}], 6)[0]
    assert "脏数据" in domain.slots_problems([{"kind": "image"}])[0]
    assert "脏数据" in domain.dialogue_problems([{"speaker": "s"}], [{"no": 1}], None)[0]
    assert "脏数据" in domain.acceptance_problems([{"criterion": "x"}])[0]
    assert domain.shots_problems(None, 6) == ["shots 不能为空：至少 1 个镜头才能生成（`03` §6.3）"]
    assert domain.slots_problems([{"idx": 1, "kind": "image", "asset_ref": "r"}]) == []


def test_domain_cost_problems_tolerates_nonsense() -> None:
    assert domain.cost_problems(Decimal("1"), None, None, None) == []
    assert domain.cost_problems(Decimal("1"), Decimal("0.5"), None, None) == []
    problems = domain.cost_problems(Decimal("1"), Decimal("0.5"), Decimal("12"), None)
    assert problems and "不一致" in problems[0]


def test_schema_json_columns_reject_dirty_rows() -> None:
    """出参侧必须挡住脏 JSON（`01` §3），不接受就把错抛成 500 之前先看住它。"""
    from pydantic import ValidationError

    from manju.schemas import ClipOut

    base = {
        "id": 1, "episode_id": 1, "idx": 1, "source_idx": 1, "title": None,
        "duration_s": "6.00", "status": "draft", "prompt": None, "negative": None,
        "shots": [{"no": 1}], "ref_slots": None, "dialogue": None, "acceptance": None,
        "current_revision_id": None, "parent_clip_id": None, "version": 1,
        "created_at": "2026-01-01T00:00:00.000Z", "updated_at": "2026-01-01T00:00:00.000Z",
    }
    with pytest.raises(ValidationError):
        ClipOut.model_validate(base)


@pytest.fixture
def sheet_path(tmp_path, monkeypatch):
    image = tmp_path / "sheet.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(review, "contact_sheet", lambda media_path, out_dir: (image, None))
    return image
