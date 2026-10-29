"""Skill 接口（`02` §3.7）。

读：任何登录用户；写：仅 admin（`02` §2.1 —— admin 对内容只读，唯独 Skill 与 provider 可写）。

写路径是一条单向门：`POST versions`（draft）→ `POST dry-run`（记 `dry_run_ok_at`）→
`POST publish`（active，旧版 superseded）。**没试跑过的版本发布不了**，所以"改完提示词
直接上线"这条路在协议层就不存在。dry-run 与真跑共用 `steps._build`，注入结果不会不一致。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from manju import skills, steps
from manju.deps import PathId, current_user, get_session, require_global_owner
from manju.errors import ApiError, ErrorCode
from manju.models import Clip, Episode, Project, Skill, SkillVersion, User
from manju.schemas import (
    DryRunIn,
    DryRunOut,
    DryRunSection,
    SkillActive,
    SkillOut,
    SkillPatchIn,
    SkillPublishOut,
    SkillRollbackIn,
    SkillVersionCreated,
    SkillVersionCreatedOut,
    SkillVersionIn,
    SkillVersionOut,
)

router = APIRouter(tags=["skills"])

PROMPT_PREVIEW = 2_000  # `02` §5.5：预览截断 2000 字
TARGETS = {"project": Project, "episode": Episode, "clip": Clip}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _skill_or_404(session: Session, skill_id: int) -> Skill:
    skill = session.get(Skill, skill_id)
    if skill is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    return skill


def _version_or_404(session: Session, skill: Skill, version: int) -> SkillVersion:
    row = session.scalar(
        select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.version == version)
    )
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    return row


def _active(session: Session, skill: Skill) -> SkillVersion | None:
    return session.scalar(
        select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.state == "active")
    )


def _target(session: Session, spec: steps.StageSpec, body: DryRunIn) -> Project | Episode | Clip:
    """dry-run 只能试注入到它真正会服务的对象上：S4 给片段、S2/S3 给单集、S1/S5 给项目。

    给错类型不是"宽容一下自己挑一个"，那会让预览与真跑的上下文对不上（`03` §6.7）。
    """
    field = f"{spec.target}_id"
    given = {name for name in ("clip_id", "episode_id", "project_id") if getattr(body, name) is not None}
    if given != {field}:
        raise ApiError(ErrorCode.VALIDATION_SHAPE, {"field": "target", "expected": field, "got": sorted(given)})
    row = session.get(TARGETS[spec.target], getattr(body, field))
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    return row


def _spec_for(skill: Skill, body: DryRunIn) -> steps.StageSpec:
    """决定"注入到哪个 stage 的上下文里"。

    `skill.stage` 只是单值标签：种子里的 `story-to-script` 一行同时服务 S1/S2/S3
    （`uk_skill_name` 让 name 唯一，拆不成三行）。所以真正决定看哪段上下文的是
    **绑定关系（`StageSpec.skill`）+ 请求给的 id 类型**，不是 `skill.stage`。
    """
    kinds = {"project_id": "project", "episode_id": "episode", "clip_id": "clip"}
    kind = next((kinds[name] for name in kinds if getattr(body, name) is not None), None)
    if kind is None:
        raise ApiError(
            ErrorCode.VALIDATION_SHAPE,
            {"field": "target", "reason": "需要 project_id / episode_id / clip_id 之一"},
        )
    bound = {spec.code: spec for spec in steps.STAGES.values() if spec.skill == skill.name}
    if not bound:
        if skill.stage not in steps.STAGES:
            raise ApiError(
                ErrorCode.VALIDATION_RANGE,
                {"field": "stage", "reason": f"{skill.stage} 没有步骤组装逻辑（质检技能只在 jobs 里用）"},
            )
        raise ApiError(
            ErrorCode.VALIDATION_RANGE,
            {"field": "stage", "reason": f"技能 {skill.name} 没有被任何 step 绑定，试跑无从谈起"},
        )
    if skill.stage in bound and bound[skill.stage].target == kind:
        return bound[skill.stage]
    for spec in bound.values():
        if spec.target == kind:
            return spec
    raise ApiError(
        ErrorCode.VALIDATION_SHAPE,
        {"field": "target", "expected": sorted({spec.target for spec in bound.values()}), "got": kind},
    )


@router.get("/skills")
def list_skills(
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[SkillOut]:
    rows = session.scalars(select(Skill).order_by(Skill.stage, Skill.name))
    return [SkillOut.model_validate(row) for row in rows]


@router.get("/skills/{skill_id}")
def get_skill(
    skill_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> SkillOut:
    return SkillOut.model_validate(_skill_or_404(session, skill_id))


@router.get("/skills/{skill_id}/versions")
def list_versions(
    skill_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[SkillVersionOut]:
    _skill_or_404(session, skill_id)
    rows = session.scalars(
        select(SkillVersion).where(SkillVersion.skill_id == skill_id).order_by(SkillVersion.version.desc())
    )
    return [SkillVersionOut.model_validate(row) for row in rows]


@router.get("/skills/{skill_id}/versions/{version}")
def get_version(
    skill_id: PathId,
    version: int,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> SkillVersionOut:
    skill = _skill_or_404(session, skill_id)
    return SkillVersionOut.model_validate(_version_or_404(session, skill, version))


@router.post("/skills/{skill_id}/versions", status_code=status.HTTP_201_CREATED)
def create_version(
    skill_id: PathId,
    body: SkillVersionIn,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> SkillVersionCreatedOut:
    skill = _skill_or_404(session, skill_id)
    require_global_owner(user)
    version, created = skills.create_version(
        session,
        skill=skill,
        body_md=body.body_md,
        created_by=user.id,
        note=body.note,
        tool_ref=body.tool_ref,
    )
    session.commit()
    if not created:
        # 同 checksum 已在库里 → 不刷版本号，回 200 让调用方知道"这条已经存在"
        response.status_code = status.HTTP_200_OK
    sections = version.sections_json or []
    return SkillVersionCreatedOut(
        skill_version=SkillVersionCreated(
            id=version.id,
            skill_id=version.skill_id,
            version=version.version,
            state=version.state,
            checksum=version.checksum,
            sections=len(sections),
            tokens_total=sum(section["tokens_est"] for section in sections),
        )
    )


@router.post("/skills/{skill_id}/versions/{version}/dry-run")
def dry_run(
    skill_id: PathId,
    version: int,
    body: DryRunIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> DryRunOut:
    skill = _skill_or_404(session, skill_id)
    require_global_owner(user)
    row = _version_or_404(session, skill, version)
    spec = _spec_for(skill, body)
    target = _target(session, spec, body)
    preview = steps.preview(session, stage=spec.code, target=target, version=row)
    if row.state == "draft":
        row.dry_run_ok_at = _now()  # 发布校验只认这个时间戳（`02` §5.5）
    session.commit()
    return DryRunOut(
        injected=[DryRunSection(**section) for section in preview["injected"]],
        budget=preview["budget"],
        prompt_preview=preview["context"][:PROMPT_PREVIEW],
        diff_vs_active=preview["diff"],
    )


@router.post("/skills/{skill_id}/versions/{version}/publish")
def publish_version(
    skill_id: PathId,
    version: int,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> SkillPublishOut:
    skill = _skill_or_404(session, skill_id)
    require_global_owner(user)
    row = _version_or_404(session, skill, version)
    superseded = skills.publish(session, row)
    session.commit()
    return SkillPublishOut(
        skill=SkillActive(id=skill.id, active_version=row.version),
        superseded=superseded.version if superseded is not None else None,
    )


@router.post("/skills/{skill_id}/rollback")
def rollback_version(
    skill_id: PathId,
    body: SkillRollbackIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> SkillPublishOut:
    skill = _skill_or_404(session, skill_id)
    require_global_owner(user)
    target, old = skills.rollback(session, skill=skill, version=body.version)
    session.commit()
    return SkillPublishOut(
        skill=SkillActive(id=skill.id, active_version=target.version),
        superseded=old.version if old is not None else None,
    )


@router.patch("/skills/{skill_id}")
def patch_skill(
    skill_id: PathId,
    body: SkillPatchIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> SkillOut:
    skill = _skill_or_404(session, skill_id)
    require_global_owner(user)
    skills.set_status(session, skill, body.status)
    session.commit()
    return SkillOut.model_validate(skill)
