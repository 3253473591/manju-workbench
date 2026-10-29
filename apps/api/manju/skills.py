"""Skill 存储与召回（`01` §4.9 / `02` §7）。

一份 Skill = 绑定 stage 的**提示词规则包**，body 按节切分，每节带 `rule_id` 与 `tags` 供召回。
`01` §3.6 要求切分与 token 估算都落在这里，所以 seeder 与运行期读的是同一份实现。

版本规则：同一 `skill` 至多一个 `active`（`uk_sv_active` 生成列保证），旧版置 `superseded`。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from manju.constants import SKILL_SECTION_MAX
from manju.errors import ApiError, ErrorCode
from manju.models import Skill, SkillVersion

RE_H2 = re.compile(r"^##\s+(.+)$")
RE_RULE_HASH = re.compile(r"#(\d{2,3})")
RE_RULE_SECTION = re.compile(r"§\s*(\d+(?:\.\d+)?)")
# Skill 是提示词规则包，落地动作由 provider 层做。正文里出现脚本围栏说明作者想塞工具调用，
# 那不是这一层能执行的东西（`02` §5.5 的 SKILL_TOOL_UNKNOWN）
RE_SCRIPT_FENCE = re.compile(r"```\s*(?:sh|bash|shell|zsh|python|py|powershell|ps1|js|javascript|ts|node)\b")

# 召回用的固定词表：只认这些词，宁可漏标也不乱标
TAGS = ("巨物", "化形", "开场", "台词", "运镜", "打斗", "形态", "参考图", "字幕", "收尾")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_sections(body_md: str) -> list[dict]:
    """按 `##` 切节。`rule_id` 优先取标题里的 `#110` / `§2.1`，否则用序号。"""
    sections: list[dict] = []
    title: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if title is None:
            return
        text = "\n".join(lines).strip()
        if not text:
            return
        hash_match = RE_RULE_HASH.search(title)
        section_match = RE_RULE_SECTION.search(title)
        if hash_match:
            rule_id = f"#{hash_match.group(1)}"
        elif section_match:
            rule_id = f"§{section_match.group(1)}"
        else:
            rule_id = f"s{len(sections) + 1}"
        sections.append(
            {
                "rule_id": rule_id,
                "title": title.strip(),
                "tags": [tag for tag in TAGS if tag in text or tag in title],
                "text": text,
                "tokens_est": max(1, len(text) // 2),  # `01` §3.6：中文按 2 字 ≈ 1 token 估
            }
        )

    for line in body_md.splitlines():
        heading = RE_H2.match(line)
        if heading:
            flush()
            title = heading.group(1).strip()
            lines = []
        elif title is not None:
            lines.append(line)
    flush()
    return sections


def upsert_skill(
    session: Session,
    *,
    name: str,
    stage: str,
    body_md: str,
    created_by: int,
    note: str | None = None,
    tool_ref: str | None = None,
) -> tuple[SkillVersion, bool]:
    """按 body 内容幂等：同 checksum 的 active 版本存在就直接返回。"""
    checksum = _checksum(body_md)
    skill = session.scalar(select(Skill).where(Skill.name == name))
    if skill is None:
        skill = Skill(name=name, stage=stage, status="active", created_at=_now())
        session.add(skill)
        session.flush()
    current = session.scalar(
        select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.state == "active")
    )
    if current is not None and current.checksum == checksum:
        return current, False

    latest = session.scalar(select(func.max(SkillVersion.version)).where(SkillVersion.skill_id == skill.id)) or 0
    version = SkillVersion(
        skill_id=skill.id,
        version=latest + 1,
        state="active",
        body_md=body_md,
        sections_json=split_sections(body_md),
        tool_ref=tool_ref,
        checksum=checksum,
        note=note,
        created_by=created_by,
        dry_run_ok_at=_now(),  # 种子等同于"已试跑通过"，否则发布校验会挡住（`02` §5.5）
        created_at=_now(),
    )
    if current is not None:
        current.state = "superseded"
        current.superseded_at = _now()
    session.add(version)
    session.flush()
    return version, True


def active_versions(
    session: Session, *, stage: str | None = None, name: str | None = None
) -> list[SkillVersion]:
    stmt = (
        select(SkillVersion)
        .join(Skill, Skill.id == SkillVersion.skill_id)
        .where(SkillVersion.state == "active", Skill.status == "active")
    )
    if stage is not None:
        stmt = stmt.where(Skill.stage == stage)
    if name is not None:
        stmt = stmt.where(Skill.name == name)
    return list(session.scalars(stmt.order_by(Skill.name)))


def rank_sections(
    versions: list[SkillVersion], *, budget: int, tags: set[str] | None = None
) -> list[dict]:
    """按标签打分排序，再按预算贪心取节。每节带 `selected` —— dry-run 要逐节解释取舍。"""

    def score(section: dict) -> int:
        return len(tags & set(section["tags"])) if tags else 0

    ranked = [{**section, "score": score(section)} for version in versions for section in version.sections_json or []]
    ranked.sort(key=lambda item: (-item["score"], item["tokens_est"]))
    used = 0
    for section in ranked:
        section["selected"] = used + section["tokens_est"] <= budget
        if section["selected"]:
            used += section["tokens_est"]
    return ranked


def select_sections(
    versions: list[SkillVersion], *, budget: int, tags: set[str] | None = None
) -> tuple[list[dict], bool]:
    """按标签打分、按预算贪心取节。返回 `(选中节, 是否因预算裁剪过)`。"""
    ranked = rank_sections(versions, budget=budget, tags=tags)
    return [section for section in ranked if section["selected"]], any(not s["selected"] for s in ranked)


def validate_body(body_md: str) -> list[dict]:
    """写入前的守卫：脚本围栏、空正文、超长单节都在这里挡掉（`02` §5.5 / `03` §6.1）。"""
    if RE_SCRIPT_FENCE.search(body_md):
        raise ApiError(ErrorCode.SKILL_TOOL_UNKNOWN, {"reason": "Skill 正文只能写规则，不能带脚本围栏"})
    sections = split_sections(body_md)
    if not sections:
        raise ApiError(ErrorCode.VALIDATION_SHAPE, {"field": "body_md", "reason": "至少要有一个 ## 节"})
    oversized = [section["rule_id"] for section in sections if len(section["text"]) > SKILL_SECTION_MAX]
    if oversized:
        raise ApiError(
            ErrorCode.VALIDATION_RANGE,
            {"field": "body_md", "reason": f"单节最多 {SKILL_SECTION_MAX} 字", "sections": oversized},
        )
    return sections


def create_version(
    session: Session,
    *,
    skill: Skill,
    body_md: str,
    created_by: int,
    note: str | None = None,
    tool_ref: str | None = None,
) -> tuple[SkillVersion, bool]:
    """新建 **draft** 版本。checksum 幂等：同内容已在库里就直接返回那条，不刷版本号。

    版本号取 `max+1`，所以先锁住 skill 行 —— 两个 admin 同时提交不能拿到同一个号。
    """
    sections = validate_body(body_md)
    checksum = _checksum(body_md)
    session.execute(select(Skill.id).where(Skill.id == skill.id).with_for_update())
    same = session.scalar(
        select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.checksum == checksum)
    )
    if same is not None:
        return same, False
    latest = session.scalar(select(func.max(SkillVersion.version)).where(SkillVersion.skill_id == skill.id)) or 0
    version = SkillVersion(
        skill_id=skill.id,
        version=latest + 1,
        state="draft",
        body_md=body_md,
        sections_json=sections,
        tool_ref=tool_ref,
        checksum=checksum,
        note=note,
        created_by=created_by,
        created_at=_now(),
    )
    session.add(version)
    session.flush()
    return version, True


def _switch(session: Session, version: SkillVersion) -> SkillVersion | None:
    """把版本置 active，原 active 转 superseded。返回被取代的那条（没有则为 None）。"""
    session.execute(select(Skill.id).where(Skill.id == version.skill_id).with_for_update())
    current = session.scalar(
        select(SkillVersion).where(
            SkillVersion.skill_id == version.skill_id, SkillVersion.state == "active"
        )
    )
    if current is not None and current.id == version.id:
        return None  # 已经是 active，重复发布是幂等的
    if version.state not in ("draft", "superseded"):
        raise ApiError(ErrorCode.CONFLICT_SKILL_ACTIVE, {"version": version.version, "state": version.state})
    if version.dry_run_ok_at is None:
        raise ApiError(ErrorCode.SKILL_NOT_DRYRUN, {"version": version.version})
    if current is not None:
        # 先腾槽位再激活：`uk_sv_active` 建在生成列上，MySQL 在**每行**写入时就校验唯一性，
        # 同一批 UPDATE 里"旧版转 superseded + 新版转 active"会撞键
        current.state = "superseded"
        current.superseded_at = _now()
        session.flush()
    version.state = "active"
    version.superseded_at = None
    session.flush()
    return current


def publish(session: Session, version: SkillVersion) -> SkillVersion | None:
    """draft → active。未经试跑的版本发布不了（`02` §6.3）。"""
    if version.state == "superseded":
        raise ApiError(ErrorCode.CONFLICT_SKILL_ACTIVE, {"version": version.version, "state": version.state})
    return _switch(session, version)


def rollback(session: Session, *, skill: Skill, version: int) -> tuple[SkillVersion, SkillVersion | None]:
    """把旧版本复归 active（`02` §6.3 的回滚口）。原 active 转 superseded。"""
    target = session.scalar(
        select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.version == version)
    )
    if target is None:
        raise ApiError(ErrorCode.NOT_FOUND, {"version": version})
    return target, _switch(session, target)


def set_status(session: Session, skill: Skill, status: str) -> Skill:
    """启用/停用整个技能。停用后 `active_versions` 就不再召回它（不用改版本状态）。"""
    skill.status = status
    session.flush()
    return skill
