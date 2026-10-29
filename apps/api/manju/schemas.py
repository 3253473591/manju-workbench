"""请求/响应模型。

传输格式（`00` §3.2 / §3.3）：时间一律 `…Z` 的 ISO 8601 毫秒串，金额与秒一律 **string**。
JSON 列在出参上都要过一遍 pydantic —— 这就是 `01` §3 说的"读取后校验，防历史脏数据"。

字段边界按 `03` §6.1 / §6.2 在这里落实（不放进 `constants.py`：它们是请求形状，不是运行参数）：
- 长度按 Unicode 码点计；`title` 先 strip 再判长度与空（§6.1-1）
- 零宽字符与控制字符拒（§6.1-2），`\\n`/`\\t` 在正文类字段例外
- 数值字符串只接受十进制字面量：科学计数法、NaN、全角数字、超存储精度一律拒（§6.2）
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, BeforeValidator, AfterValidator
from pydantic_core import PydanticCustomError

from manju.constants import SKILL_BODY_MAX

T = TypeVar("T")

# `[0-9]` 而不是 `\d`：Python 的 `\d` 会匹配全角数字，那正是要拒的东西
_DECIMAL_TEXT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_INVISIBLE = re.compile(r"[\u200b-\u200d\u202a-\u202e\u2066-\u2069]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _utc(value: datetime) -> str:
    naive = value.replace(tzinfo=None) if value.tzinfo else value
    return naive.replace(tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _decimal_text(value: object, places: int) -> Decimal:
    text = str(value).strip()
    if not _DECIMAL_TEXT.fullmatch(text):
        raise PydanticCustomError("decimal_format", "只接受十进制数字符串（不接受科学计数法、NaN、全角数字）")
    if len(text.partition(".")[2]) > places:
        raise PydanticCustomError("decimal_digits", "小数位超过存储精度，请显式给值")
    return Decimal(text)


def _reject_hidden(text: str) -> None:
    if _INVISIBLE.search(text):
        raise PydanticCustomError("invisible_char", "包含零宽或双向控制字符")
    if _CONTROL.search(text):
        raise PydanticCustomError("control_char", "包含控制字符")


def _identifier(max_length: int):
    """标识性字段：strip + 非空 + 长度 + 不可见字符。"""

    def check(value: str) -> str:
        text = value.strip()
        if not text:
            raise PydanticCustomError("blank", "不能是空白字符串")
        if len(text) > max_length:
            raise PydanticCustomError("string_too_long", f"最多 {max_length} 字")
        _reject_hidden(text)
        return text

    return check


def _blob(max_length: int):
    """正文类字段：允许空（`03` §6.1 这些字段最小长度是 0），但控制字符只放 `\\n` `\\t`。"""

    def check(value: str) -> str:
        if len(value) > max_length:
            raise PydanticCustomError("string_too_long", f"最多 {max_length} 字符")
        _reject_hidden(value)
        return value

    return check


def _email(value: str) -> str:
    text = value.strip().lower()
    if "@" not in text or text.startswith("@") or text.endswith("@"):
        raise PydanticCustomError("invalid_email", "邮箱格式不正确")
    if not 5 <= len(text) <= 190:
        raise PydanticCustomError("string_too_long", "邮箱长度需在 5–190 之间")
    _reject_hidden(text)
    return text


UtcDateTime = Annotated[datetime, PlainSerializer(_utc, return_type=str, when_used="json")]
Money = Annotated[
    Decimal,
    BeforeValidator(lambda v: _decimal_text(v, 6)),
    PlainSerializer(lambda v: f"{v:.6f}", return_type=str, when_used="json"),
]
Seconds = Annotated[
    Decimal,
    BeforeValidator(lambda v: _decimal_text(v, 2)),
    PlainSerializer(lambda v: f"{v:.2f}", return_type=str, when_used="json"),
]
Title = Annotated[str, AfterValidator(_identifier(200))]
Email = Annotated[str, AfterValidator(_email)]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: int | None = None


# ---------------------------------------------------------------- 认证


class LoginIn(ApiModel):
    """登录不套注册的长度规则：口令太短应回 401，不该回 422 把格式错误暴露给猜测者。"""

    email: Email
    password: Annotated[str, Field(min_length=1, max_length=72)]


class RegisterIn(ApiModel):
    email: Email
    password: Annotated[str, Field(min_length=10, max_length=72)]


class UserOut(OutModel):
    id: int
    email: str
    role: Literal["user", "lead", "admin"]
    created_at: UtcDateTime


class Capabilities(BaseModel):
    can_create_project: bool
    can_write_global: bool
    can_write_any_project: bool


class MeOut(BaseModel):
    user: UserOut
    capabilities: Capabilities


# ---------------------------------------------------------------- 项目


class ProjectCreate(ApiModel):
    title: Title
    genre: Annotated[str, AfterValidator(_blob(24))] | None = None
    tone: Annotated[str, AfterValidator(_blob(120))] | None = None
    logline: Annotated[str, AfterValidator(_blob(500))] | None = None


class ProjectUpdate(ApiModel):
    title: Title | None = None
    genre: Annotated[str, AfterValidator(_blob(24))] | None = None
    tone: Annotated[str, AfterValidator(_blob(120))] | None = None
    logline: Annotated[str, AfterValidator(_blob(500))] | None = None
    status: Literal["active", "archived"] | None = None


class ProjectOut(OutModel):
    id: int
    title: str
    owner_id: int
    genre: str | None
    tone: str | None
    logline: str | None
    status: Literal["active", "archived"]
    version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class MemberIn(ApiModel):
    email: Email
    role: Literal["member", "viewer"] = "member"


class MemberOut(BaseModel):
    user_id: int
    email: str
    role: Literal["member", "viewer"]


# ---------------------------------------------------------------- 集与片段


class EpisodeCreate(ApiModel):
    title: Annotated[str, AfterValidator(_blob(200))] | None = None
    synopsis: Annotated[str, AfterValidator(_blob(200_000))] | None = None


class EpisodeUpdate(ApiModel):
    title: Annotated[str, AfterValidator(_blob(200))] | None = None
    synopsis: Annotated[str, AfterValidator(_blob(200_000))] | None = None


class EpisodeOut(OutModel):
    id: int
    project_id: int
    idx: int
    title: str | None
    synopsis: str | None
    status: str
    version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class Shot(ApiModel):
    no: Annotated[int, Field(ge=1, le=8)]
    start: Seconds
    end: Seconds
    desc: Annotated[str, AfterValidator(_identifier(2000))]
    camera: Annotated[str, AfterValidator(_blob(200))] | None = None


class Slot(ApiModel):
    idx: Annotated[int, Field(ge=1, le=30)]
    kind: Literal["image", "video", "audio"]
    asset_id: int | None = None
    asset_ref: Annotated[str, AfterValidator(_blob(512))] | None = None
    # 这个槽位由剧本哪条「参考资产」回填而来（01 §3.1）；只做追溯，不参与校验
    script_ref: Annotated[str, AfterValidator(_blob(512))] | None = None
    role: Literal["实景", "造型锚", "构图锚", "形态参考"]
    note: Annotated[str, AfterValidator(_blob(500))] = ""
    # 出参派生（02 §3.3）：`asset_id` 非空时直接给出可用的取图地址，省掉前端 N+1
    url: str | None = None
    thumb_url: str | None = None


class Dialogue(ApiModel):
    speaker: Annotated[str, AfterValidator(_identifier(40))]
    text: Annotated[str, AfterValidator(_identifier(200))]
    shot_no: int | None = None
    on_screen: bool = True


class Acceptance(ApiModel):
    id: Annotated[str, AfterValidator(_identifier(8))]
    criterion: Annotated[str, AfterValidator(_identifier(200))]
    superseded_by: Annotated[str, AfterValidator(_blob(8))] | None = None


class ClipCreate(ApiModel):
    duration_s: Annotated[Decimal, Field(ge=Decimal("1.00"), le=Decimal("15.00")), BeforeValidator(
        lambda v: _decimal_text(v, 2)
    )]
    source_idx: Annotated[int, Field(ge=1, le=30)] | None = None
    title: Annotated[str, AfterValidator(_identifier(200))] | None = None
    prompt: Annotated[str, AfterValidator(_blob(20_000))] | None = None
    negative: Annotated[str, AfterValidator(_blob(2_000))] | None = None
    shots: list[Shot] | None = None
    ref_slots: list[Slot] | None = None
    dialogue: list[Dialogue] | None = None
    acceptance: list[Acceptance] | None = None


class ClipUpdate(ApiModel):
    duration_s: Annotated[Decimal, Field(ge=Decimal("1.00"), le=Decimal("15.00")), BeforeValidator(
        lambda v: _decimal_text(v, 2)
    )] | None = None
    # 状态只能沿 `02` §6.2 的迁移图走，非法跳转由 domain 判成 409
    status: Literal["draft", "prompted", "generating", "review", "approved", "rejected"] | None = None
    title: Annotated[str, AfterValidator(_identifier(200))] | None = None
    prompt: Annotated[str, AfterValidator(_blob(20_000))] | None = None
    negative: Annotated[str, AfterValidator(_blob(2_000))] | None = None
    shots: list[Shot] | None = None
    ref_slots: list[Slot] | None = None
    dialogue: list[Dialogue] | None = None
    acceptance: list[Acceptance] | None = None


class ClipOut(BaseModel):
    id: int
    episode_id: int
    idx: int
    source_idx: int
    title: str | None
    duration_s: Seconds
    status: str
    prompt: str | None
    negative: str | None
    shots: list[Shot] | None
    ref_slots: list[Slot] | None
    dialogue: list[Dialogue] | None
    acceptance: list[Acceptance] | None
    current_revision_id: int | None
    output_asset_id: int | None = None  # 派生：当前 revision 的产物资产（镜头墙海报用）
    cost_total: Money = Decimal("0")  # 派生：该片段所有 revision 的 cost 之和
    parent_clip_id: int | None
    version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ClipRollbackIn(ApiModel):
    """回滚到某一次产物（P0-8）。`revision_id` 必须是本片段的 take。"""

    revision_id: Annotated[int, Field(ge=1)]


# ---------------------------------------------------------------- 版本与成本


class Motion(BaseModel):
    samples: int | None = None
    freeze: int | None = None
    avg: float | None = None
    max: float | None = None


class AcceptanceResult(BaseModel):
    id: str
    result: Literal["pass", "fail", "unknown"]
    evidence: str = ""


class RevisionOut(BaseModel):
    id: int
    seq: int | None = None  # 派生：同一 target 内的第几版（1-based），前端展示 "v3" 用
    target_kind: str
    target_id: int
    prompt: str | None
    negative: str | None
    params: dict | None
    provider: str | None
    model_requested: str | None
    model_used: str | None
    skill_versions: dict | None
    input_tokens: int | None
    output_tokens: int | None
    video_seconds: Seconds | None
    unit_price: Money | None
    cost: Money
    latency_ms: int | None
    motion: Motion | None
    acceptance_result: list[AcceptanceResult] | None
    parent_revision_id: int | None
    output_asset_id: int | None
    created_at: UtcDateTime


class CostItem(BaseModel):
    key: str
    calls: int
    tokens: int
    seconds: Seconds
    cost: Money


class CostTotal(BaseModel):
    calls: int
    cost: Money


class BudgetOut(BaseModel):
    spent_today: Money
    limit: Money
    ratio: float
    tier: str  # ok | warn | over


class CostOut(BaseModel):
    group_by: str
    items: list[CostItem]
    total: CostTotal
    budget: BudgetOut


class ReadinessCheckOut(BaseModel):
    id: str
    label: str
    ok: bool
    detail: dict


class ReadinessOut(BaseModel):
    episode_id: int
    status: str
    ready: bool
    checks: list[ReadinessCheckOut]
    blockers: list[str]
    checked_at: datetime


class PublishOut(BaseModel):
    episode: EpisodeOut
    readiness: ReadinessOut


class LessonOut(OutModel):
    id: int
    scope: str
    failure_type: str
    symptom: str
    fix: str
    evidence_revision_id: int | None
    hit_count: int
    last_hit_at: UtcDateTime


class RevisionDiff(BaseModel):
    a: int
    b: int
    prompt_diff: str
    # 参数是各 stage 自己写进 params_json 的任意 JSON（prompt 可能是对象、skill_sections 是整数），
    # 这里只做 before/after 透出，不替调用方猜类型
    params_diff: dict[str, list[Any]]
    cost_delta: Money
    skill_versions_diff: dict[str, list[int | None]]


# ---------------------------------------------------------------- 资产


class AssetOut(BaseModel):
    """`url` / `thumb_url` 是本服务的取图地址（`02` §3.9），不是磁盘路径。

    磁盘路径只给后端与导入脚本用；前端一律走 `url`，这样换存储时前端不用改。
    """

    id: int
    project_id: int
    kind: Literal["image", "video", "audio"]
    path: str
    thumb_path: str | None
    bytes: int
    w: int | None
    h: int | None
    thumb_w: int | None
    thumb_h: int | None
    dur_s: Seconds | None
    source: Literal["gen", "upload", "seed"]
    created_at: UtcDateTime
    url: str
    thumb_url: str


# ---------------------------------------------------------------- 协作


class LockIn(ApiModel):
    target_kind: Literal["clip", "episode", "skill_version"]
    target_id: Annotated[int, Field(ge=1)]


class LockOut(BaseModel):
    target_kind: str
    target_id: int
    user_id: int
    expires_at: UtcDateTime


class HeartbeatIn(ApiModel):
    project_id: Annotated[int, Field(ge=1)]
    focus_target: Annotated[str, AfterValidator(_blob(40))] | None = None


class PresenceOut(BaseModel):
    user_id: int
    email: str
    focus_target: str | None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    db: bool
    migrated: bool


# ---------------------------------------------------------------- Skill


class SkillOut(OutModel):
    id: int
    name: str
    stage: Literal["S1", "S2", "S3", "S4", "S5", "review"]
    status: Literal["active", "disabled"]
    created_at: UtcDateTime


class SkillVersionOut(OutModel):
    id: int
    skill_id: int
    version: int
    state: Literal["draft", "active", "superseded"]
    sections: list[dict] = Field(validation_alias="sections_json")
    tool_ref: str | None
    checksum: str
    note: str | None
    dry_run_ok_at: UtcDateTime | None
    superseded_at: UtcDateTime | None
    created_at: UtcDateTime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class SkillVersionIn(ApiModel):
    """新建版本。`body_md` 由服务端按 `##` 切节并算 tokens（`02` §5.5）。"""

    body_md: Annotated[str, Field(min_length=1, max_length=SKILL_BODY_MAX)]
    note: Annotated[str | None, Field(default=None, max_length=200)]
    tool_ref: Annotated[str | None, Field(default=None, max_length=64)]


class SkillVersionCreated(BaseModel):
    id: int
    skill_id: int
    version: int
    state: Literal["draft", "active", "superseded"]
    checksum: str
    sections: int
    tokens_total: int


class SkillVersionCreatedOut(BaseModel):
    skill_version: SkillVersionCreated


class DryRunIn(ApiModel):
    """试跑目标：给哪个对象的上下文里试注入。三者只填一个，且要与技能 stage 对得上。"""

    clip_id: int | None = None
    episode_id: int | None = None
    project_id: int | None = None


class DryRunSection(BaseModel):
    rule_id: str
    title: str
    tokens_est: int
    selected: bool


class DryRunBudget(BaseModel):
    input_limit: int
    used_by_skill: int
    used_total: int
    trimmed: bool


class DryRunDiff(BaseModel):
    added_rules: list[str]
    removed_rules: list[str]
    tokens_delta: int


class DryRunOut(BaseModel):
    injected: list[DryRunSection]
    budget: DryRunBudget
    prompt_preview: str
    diff_vs_active: DryRunDiff


class SkillActive(BaseModel):
    id: int
    active_version: int


class SkillPublishOut(BaseModel):
    skill: SkillActive
    superseded: int | None


class SkillRollbackIn(ApiModel):
    version: Annotated[int, Field(ge=1)]


class SkillPatchIn(ApiModel):
    status: Literal["active", "disabled"]


class StepTemplateOut(BaseModel):
    stage: str
    label: str
    target: str
    skill: str | None
    sections: int
    tokens_est: int


class StepRunOut(BaseModel):
    stage: str
    revision_id: int
    payload: dict
    written: list[str]
    input_tokens: int
    output_tokens: int
    cost: Money
    skill_sections: int
    context_trimmed: bool


# ---------------------------------------------------------------- 生成与 job


class GenerateIn(ApiModel):
    res: Literal["draft", "final"] = "draft"
    budget_limit: Money | None = None


class TraceOut(OutModel):
    """job 执行时间线的一条（`02` §5.4）。`detail` 只放稳定字段，前端直接渲染键值。"""

    ts: UtcDateTime
    step: str
    verdict: str | None
    ms: int | None
    detail: dict | None


class JobOut(OutModel):
    id: int
    kind: str
    target_kind: str
    target_id: int
    state: str
    attempts: int
    max_attempts: int
    budget_limit: Money
    cost_used: Money
    provider: str | None
    model: str | None
    error_code: str | None
    error_detail: dict | None
    created_at: UtcDateTime
    started_at: UtcDateTime | None
    finished_at: UtcDateTime | None


class GeneratePreview(BaseModel):
    prompt_sha256: str
    prompt_head: str
    tokens_est: int
    cost_est: Money
    duration_s: Seconds


class GenerateOut(BaseModel):
    job: JobOut
    preview: GeneratePreview
