"""`steps/*/run` 五条路由（`02` §3.4）。

五条路由各自鉴权、共用 `steps.run_step` 一条代码路径 —— 这正是 P0-4 的 DoD
"5 环节共用一条代码路径，模板热改不发版"。

与协议的已知偏差：`02` §3.4 把这几条标了 `I`（要幂等键）。文本 step 目前是**同步**执行、
成本为 0、且写回是可覆盖的字段，加幂等键收益为零；等文本单价落地、需要防重复扣费时再补。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from manju import skills, steps
from manju.api.episodes import _clip_for_write, _episode_for_write
from manju.config import Settings
from manju.deps import PathId, current_user, get_session, get_settings, project_for_write
from manju.models import Episode, Project, User
from manju.schemas import StepRunOut, StepTemplateOut

router = APIRouter(tags=["steps"])


def _out(result) -> StepRunOut:
    revision, model, section_count, trimmed, written = result
    return StepRunOut(
        stage=str((revision.params_json or {}).get("stage")),
        revision_id=revision.id,
        payload=model.model_dump(mode="json"),
        written=written,
        input_tokens=revision.input_tokens or 0,
        output_tokens=revision.output_tokens or 0,
        cost=revision.cost,
        skill_sections=section_count,
        context_trimmed=trimmed,
    )


@router.get("/steps/templates")
def list_templates(
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[StepTemplateOut]:
    rows: list[StepTemplateOut] = []
    for spec in steps.STAGES.values():
        versions = skills.active_versions(session, name=spec.skill) if spec.skill else []
        sections = sum(len(version.sections_json or []) for version in versions)
        tokens = sum(
            section["tokens_est"] for version in versions for section in (version.sections_json or [])
        )
        rows.append(
            StepTemplateOut(
                stage=spec.code,
                label=spec.label,
                target=spec.target,
                skill=spec.skill,
                sections=sections,
                tokens_est=tokens,
            )
        )
    return rows


def _project_for_step(session: Session, user: User, project_id: int) -> Project:
    return project_for_write(session, user, project_id)


@router.post("/projects/{project_id}/steps/S1/run")
def run_s1(
    project_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> StepRunOut:
    project = _project_for_step(session, user, project_id)
    result = steps.run_step(session, settings, stage="S1", target=project)
    session.commit()
    return _out(result)


@router.post("/projects/{project_id}/steps/S5/run")
def run_s5(
    project_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> StepRunOut:
    project = _project_for_step(session, user, project_id)
    result = steps.run_step(session, settings, stage="S5", target=project)
    session.commit()
    return _out(result)


@router.post("/episodes/{episode_id}/steps/S2/run")
def run_s2(
    episode_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> StepRunOut:
    episode = _episode_for_write(session, user, episode_id)
    result = steps.run_step(session, settings, stage="S2", target=episode)
    session.commit()
    return _out(result)


@router.post("/episodes/{episode_id}/steps/S3/run")
def run_s3(
    episode_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> StepRunOut:
    episode: Episode = _episode_for_write(session, user, episode_id)
    result = steps.run_step(session, settings, stage="S3", target=episode)
    session.commit()
    return _out(result)


@router.post("/clips/{clip_id}/steps/S4/run")
def run_s4(
    clip_id: PathId,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> StepRunOut:
    clip = _clip_for_write(session, user, clip_id)
    result = steps.run_step(session, settings, stage="S4", target=clip)
    session.commit()
    return _out(result)
