"""LLMStep：5 个环节共用一条代码路径（`02` §3.4 / PRD P0-4）。

一条路径 = 组装上下文（带 12k 预算与裁剪）→ provider.text → 结构化校验 → 落 revision → 写回字段。
五个 stage 的差别只有三样：目标对象、绑定的 Skill、输出 schema —— 都在这张表里。

S5（人物卡）只产出不落库：人物卡要生成的其实是**图**，而 `provider.image` 还没实现，
写进 project/episode 的哪个字段都不对。响应里带回角色卡，等资产库落地。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from manju import clip_rules, runtime, lessons, skills
from manju.config import Settings
from manju.constants import CTX_INPUT_LIMIT
from manju.errors import ApiError, ErrorCode
from manju.models import Clip, Episode, Project, Revision, SkillVersion
from manju.providers import Provider, ProviderError, TextResult, make_provider
from manju.schemas import Acceptance, Dialogue, Shot

SKILL_BUDGET = 2_500  # 上下文里留给 Skill 的预算（`01` §6.1 的第 3 档）


class S1Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logline: str = Field(min_length=1, max_length=500)
    genre: str | None = Field(default=None, max_length=24)
    tone: str | None = Field(default=None, max_length=120)


class S2Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    synopsis: str = Field(min_length=1, max_length=200_000)


class S3Clip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idx: int = Field(ge=1, le=30)
    title: str = Field(min_length=1, max_length=200)
    duration_s: Decimal = Field(ge=Decimal("1.00"), le=Decimal("15.00"))
    shots: list[Shot]
    dialogue: list[Dialogue] = Field(default_factory=list)
    acceptance: list[Acceptance] = Field(default_factory=list)


class S3Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clips: list[S3Clip] = Field(min_length=1, max_length=30)


class S4Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=20_000)
    negative: str | None = Field(default=None, max_length=2_000)


class Character(BaseModel):
    """人物卡。字段必须**强制**：S5 早期用裸 `list[dict]`，真模型会返回 `{"name": …}` 把
    `anchor` 省掉，于是下游拿到一堆 `anchor=None`（实测）。结构靠规则，不靠模型自觉。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=40)
    anchor: str = Field(min_length=1, max_length=500)


class S5Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    characters: list[Character] = Field(min_length=1)


@dataclass(frozen=True)
class StageSpec:
    code: str
    label: str
    target: str  # project | episode | clip
    skill: str | None
    tags: tuple[str, ...]
    schema: type[BaseModel]


STAGES: dict[str, StageSpec] = {
    stage.code: stage
    for stage in (
        StageSpec("S1", "总大纲与设定", "project", "story-to-script", ("开场",), S1Out),
        StageSpec("S2", "分集剧情", "episode", "story-to-script", ("台词",), S2Out),
        StageSpec("S3", "分集剧本分镜", "episode", "story-to-script", ("运镜", "台词"), S3Out),
        StageSpec("S4", "终剪提示词", "clip", "seedance-final-prompt", ("运镜", "台词"), S4Out),
        StageSpec("S5", "人物卡", "project", "concept-sheet", ("形态", "参考图"), S5Out),
    )
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Context:
    """组装结果。`sections` 保留每节的选中标记，dry-run 要把"为什么这节没进去"原样讲出来。"""

    text: str
    tokens: int
    sections: list[dict]
    trimmed: bool

    @property
    def chosen(self) -> list[dict]:
        return [section for section in self.sections if section["selected"]]


def _build(
    session: Session,
    spec: StageSpec,
    target: Project | Episode | Clip,
    *,
    override: SkillVersion | None = None,
) -> Context:
    """项目卡 + 前序产物 + 命中的 Skill 节。超预算先裁 Skill（`03` §4.3）。

    `override` 让 dry-run 用待发布的草稿版本替换同技能的生产版本 —— 组装代码只有这一条路径，
    所以"试跑看到的注入"与"真跑注入的"不可能不一致。
    """
    lines: list[str] = [f"# {spec.code} {spec.label}"]
    if isinstance(target, Project):
        lines.append(f"项目：《{target.title}》｜类型 {target.genre or '未定'}｜基调 {target.tone or '未定'}")
        if target.logline:
            lines.append(f"已有概述：{target.logline}")
    elif isinstance(target, Episode):
        project = session.get(Project, target.project_id)
        lines.append(f"项目：《{project.title}》｜类型 {project.genre or '未定'}")
        if project.logline:
            lines.append(f"项目概述：{project.logline}")
        lines.append(f"第 {target.idx} 集：{target.title or '未命名'}")
        if target.synopsis:
            lines.append(f"分集剧情：{target.synopsis[:2000]}")
    else:
        episode = session.get(Episode, target.episode_id)
        project = session.get(Project, episode.project_id)
        lines.append(f"项目：《{project.title}》")
        lines.append(f"片段 {target.idx}：{target.title or '未命名'}｜时长 {target.duration_s}s")
        # S4 的产物必须过 domain 不变量（台词逐字内嵌、分镜闭合），所以**必须先把它看到**。
        # 早期只喂了 prompt 不喂 shots/dialogue，真模型无从内嵌，必然 422（实测）。
        for shot in target.shots_json or []:
            lines.append(f"镜头{shot.get('no')}：{shot.get('start')}–{shot.get('end')}s｜{shot.get('desc')}")
        if target.dialogue_json:
            lines.append("台词（必须逐字出现在提示词正文里，一个字都不能改）：")
            for item in target.dialogue_json:
                where = f"镜头{item['shot_no']}" if item.get("shot_no") else "旁白"
                lines.append(f"- {item.get('speaker')}（{where}）：「{item.get('text')}」")
        if target.acceptance_json:
            lines.append("需满足的画面要求：")
            for item in target.acceptance_json:
                lines.append(f"- {item.get('criterion')}")
        if target.prompt:
            lines.append(f"既有提示词：{target.prompt[:1500]}")

    if override is not None:
        bound = [override]
    elif spec.skill:
        ids = _skill_ids(session, spec.skill)
        bound = [version for version in skills.active_versions(session) if version.skill_id in ids]
    else:
        bound = []
    ranked = skills.rank_sections(bound, budget=SKILL_BUDGET, tags=set(spec.tags))
    chosen = [section for section in ranked if section["selected"]]
    if chosen:
        lines.append("\n## 适用规则（Skill 命中节）")
        for section in chosen:
            lines.append(f"### [{section['rule_id']}] {section['title']}\n{section['text']}")
    # 失败教训按作用域召回：写了记忆却不在下一次组装时读出来，记忆就是摆设（`03` §4.3）
    for lesson in lessons.recall(session, _scope(session, target), limit=3):
        lines.append(f"- 踩过的坑[{lesson.failure_type}]：{lesson.symptom} → {lesson.fix}")
    text = "\n".join(lines)
    return Context(
        text=text,
        tokens=len(text) // 2,
        sections=ranked,
        trimmed=any(not section["selected"] for section in ranked),
    )


def _scope(session: Session, target: Project | Episode | Clip) -> str:
    if isinstance(target, Project):
        return f"project:{target.id}"
    if isinstance(target, Episode):
        return f"project:{target.project_id}"
    episode = session.get(Episode, target.episode_id)
    return f"project:{episode.project_id}"


def _skill_ids(session: Session, name: str | None) -> set[int]:
    if name is None:
        return set()
    from manju.models import Skill

    ids = session.scalars(select(Skill.id).where(Skill.name == name))
    return set(ids)


def _validate(spec: StageSpec, payload: dict) -> BaseModel:
    try:
        return spec.schema.model_validate(payload)
    except ValidationError as exc:
        raise ApiError(ErrorCode.VALIDATION_INVARIANT, {"stage": spec.code, "problems": [str(exc)[:400]]}) from exc


EPISODE_ORDER = ("draft", "scripted", "storyboarded", "producing", "done")


def _advance_episode(episode: Episode, target: str) -> None:
    """只往前推：S2 出剧情 → scripted，S3 出分镜 → storyboarded。

    `ck_ep_status` 有五个态，之前没有任何 pipeline 代码写它，集列表永远显示 draft。
    """
    current = EPISODE_ORDER.index(episode.status) if episode.status in EPISODE_ORDER else 0
    if EPISODE_ORDER.index(target) > current:
        episode.status = target
        episode.updated_at = _now()


def _format_hint(spec: StageSpec) -> str:
    """给真模型的输出契约。不给它，模型会自己包一层信封（实测 DeepSeek 干过）。

    直接用 pydantic 的 JSON Schema：字段名与约束只有一处真相，改 schema 不用改这段。
    """
    return json.dumps(spec.schema.model_json_schema(), ensure_ascii=False)


def _write_back(
    session: Session, spec: StageSpec, target: Project | Episode | Clip, model: BaseModel
) -> list[str]:
    """把结构化结果写回它该在的字段，并走状态机/不变量。"""
    written: list[str] = []
    if spec.code == "S1":
        assert isinstance(target, Project) and isinstance(model, S1Out)
        target.logline = model.logline
        target.genre = model.genre or target.genre
        target.tone = model.tone or target.tone
        target.version += 1
        target.updated_at = _now()
        written.extend(["project.logline", "project.genre", "project.tone"])
    elif spec.code == "S2":
        assert isinstance(target, Episode) and isinstance(model, S2Out)
        # 已有分镜再重跑剧情：synopsis 会被改写，而片段还是按旧剧情生成的 —— 静默不一致。
        # 和 S3 的 409 同一口径：要重来请新建一集。
        existing_clips = session.scalar(
            select(func.count()).select_from(Clip).where(Clip.episode_id == target.id)
        )
        if existing_clips:
            raise ApiError(
                ErrorCode.CONFLICT_CLIP_INDEX,
                {"reason": "该集已有分镜，重跑剧情会让剧情与分镜不一致；请新建一集", "clips": existing_clips},
            )
        target.synopsis = model.synopsis
        target.version += 1
        target.updated_at = _now()
        _advance_episode(target, "scripted")
        written.append("episode.synopsis")
    elif spec.code == "S3":
        assert isinstance(target, Episode) and isinstance(model, S3Out)
        session.execute(select(Episode.id).where(Episode.id == target.id).with_for_update())
        if len({item.idx for item in model.clips}) != len(model.clips):
            raise ApiError(ErrorCode.VALIDATION_INVARIANT, {"reason": "分镜序号重复"})
        existing = len(session.scalars(select(Clip.id).where(Clip.episode_id == target.id).with_for_update()).all())
        if existing:
            raise ApiError(
                ErrorCode.CONFLICT_CLIP_INDEX,
                {"reason": "该集已有片段，重跑分镜请新建一集", "existing": existing},
            )
        project_id = target.project_id
        for item in model.clips:
            data = item.model_dump(mode="json")
            # 逐片段过 `01` §5.4 的不变量：分镜不闭合/台词未内嵌会在这里被拒
            clip_rules.enforce_clip(
                session,
                project_id=project_id,
                duration_s=item.duration_s,
                shots=data["shots"],
                ref_slots=None,
                dialogue=data["dialogue"],
                acceptance=data["acceptance"],
                prompt=None,
                require_shots=True,
            )
            session.add(
                Clip(
                    episode_id=target.id,
                    idx=item.idx,
                    source_idx=item.idx,
                    title=item.title,
                    duration_s=item.duration_s,
                    status="draft",
                    shots_json=data["shots"],
                    dialogue_json=data["dialogue"] or None,
                    acceptance_json=data["acceptance"] or None,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        written.append(f"episode.clips×{len(model.clips)}")
        _advance_episode(target, "storyboarded")
    elif spec.code == "S4":
        assert isinstance(target, Clip) and isinstance(model, S4Out)
        episode = session.get(Episode, target.episode_id)
        clip_rules.enforce_clip(
            session, project_id=episode.project_id, duration_s=target.duration_s,
            shots=target.shots_json, ref_slots=target.ref_slots_json, dialogue=target.dialogue_json,
            acceptance=target.acceptance_json, prompt=model.prompt, require_shots=True,
        )
        if target.status in ("draft", "prompted"):
            clip_rules.enforce_transition(target.status, "prompted")
            target.status = "prompted"
        target.prompt = model.prompt
        target.negative = model.negative
        target.version += 1
        target.updated_at = _now()
        written.extend(["clip.prompt", "clip.negative"])
    return written


def run_step(
    session: Session,
    settings: Settings,
    *,
    stage: str,
    target: Project | Episode | Clip,
    provider: Provider | None = None,
) -> tuple[Revision, BaseModel, int, bool, list[str]]:
    spec = STAGES.get(stage)
    if spec is None:
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"field": "stage", "allowed": sorted(STAGES)})
    # 通道被管理员停用 → 在花钱之前就拒（配置在环境变量、运行态在这张表，见 `runtime`）
    runtime.assert_enabled(session, "text")
    context = _build(session, spec, target)
    if context.tokens > CTX_INPUT_LIMIT:
        raise ApiError(ErrorCode.INTERNAL_STATE, {"reason": "上下文裁剪后仍超预算", "tokens": context.tokens})

    provider = provider or make_provider(settings)
    result: TextResult
    try:
        result = provider.text(stage=stage, context=context.text, format_hint=_format_hint(spec))
    except ProviderError as exc:
        raise ApiError(ErrorCode.PROVIDER_UNAVAILABLE, {"message": str(exc), "retryable": exc.retryable}) from exc
    model = _validate(spec, result.payload)

    revision = Revision(
        target_kind=spec.target,
        target_id=target.id,
        prompt=context.text,
        params_json={
            "stage": stage,
            "skill_sections": len(context.chosen),
            "trimmed": context.trimmed,
            "payload": result.payload,
        },
        provider=provider.name,
        model_requested="mock-text",
        model_used=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost=result.cost,
        latency_ms=result.latency_ms,
        created_at=_now(),
    )
    session.add(revision)
    session.flush()

    written = _write_back(session, spec, target, model)
    session.flush()
    return revision, model, len(context.chosen), context.trimmed, written


def preview(session: Session, *, stage: str, target: Project | Episode | Clip, version: SkillVersion) -> dict:
    """dry-run：只组装不调用（`02` §5.5）。不产生 revision、不扣费、不写项目字段。

    对比基线取同技能的 active 版本，且只比这个技能自己的规则 —— 预算挤掉别人家的节会在
    `budget.trimmed` 里体现，混进 diff 只会让人误读成"这条规则被删了"。
    """
    spec = STAGES.get(stage)
    if spec is None:
        raise ApiError(ErrorCode.VALIDATION_RANGE, {"field": "stage", "allowed": sorted(STAGES)})
    draft = _build(session, spec, target, override=version)
    active = session.scalar(
        select(SkillVersion).where(
            SkillVersion.skill_id == version.skill_id, SkillVersion.state == "active"
        )
    )
    baseline: dict[str, int] = {}
    if active is not None:
        baseline = {
            section["rule_id"]: section["tokens_est"]
            for section in _build(session, spec, target, override=active).chosen
        }
    own = {section["rule_id"] for section in version.sections_json or []}
    current = {
        section["rule_id"]: section["tokens_est"] for section in draft.chosen if section["rule_id"] in own
    }
    return {
        "injected": draft.sections,
        "budget": {
            "input_limit": CTX_INPUT_LIMIT,
            "used_by_skill": sum(section["tokens_est"] for section in draft.chosen),
            "used_total": draft.tokens,
            "trimmed": draft.trimmed,
        },
        "context": draft.text,
        "diff": {
            "added_rules": [rule for rule in current if rule not in baseline],
            "removed_rules": [rule for rule in baseline if rule not in current],
            "tokens_delta": sum(current.values()) - sum(baseline.values()),
        },
    }
