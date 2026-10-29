"""发布门：一集能不能标记为 done，由四道检查决定（不是由按钮决定）。

四个维度（对应 `PRD` 的 P1-3）：
1. 片段产物完整 —— 每个片段都有当前 take 的产物；
2. 审批就绪 —— 全部片段 approved（review/rejected 都不算）；
3. 质检可信 —— 没有任何 fail，未判率 ≤ 20%；
4. 预算档位 —— 项目当天花费未到 `over`（warn 可以发，但要在面板上看见）。

预算三档：ok < 70% ≤ warn < 100% ≤ over。到 over 时新的提交本来就会被 `jobs.submit` 拒绝，
发布门用同一档位判断，避免"钱花了但界面没提示"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from manju.constants import BUDGET_PROJECT_DAILY
from manju.jobs import project_spent_today
from manju.models import Clip, Episode, Revision

WARN_RATIO = 0.7
UNKNOWN_RATIO_MAX = 0.2


@dataclass
class Check:
    id: str
    label: str
    ok: bool
    detail: dict


def budget_status(session: Session, project_id: int) -> dict:
    spent = project_spent_today(session, project_id)
    limit = BUDGET_PROJECT_DAILY
    ratio = float(spent / limit) if limit else 0.0
    if spent >= limit:
        tier = "over"
    elif ratio >= WARN_RATIO:
        tier = "warn"
    else:
        tier = "ok"
    return {"spent_today": spent, "limit": limit, "ratio": round(ratio, 4), "tier": tier}


def episode_readiness(session: Session, episode: Episode) -> dict:
    clips = list(session.scalars(select(Clip).where(Clip.episode_id == episode.id).order_by(Clip.idx)))
    revision_ids = [clip.current_revision_id for clip in clips if clip.current_revision_id]
    revisions = {
        row.id: row
        for row in session.scalars(select(Revision).where(Revision.id.in_(revision_ids))).all()
    } if revision_ids else {}

    with_output = sum(
        1 for clip in clips
        if clip.current_revision_id and revisions.get(clip.current_revision_id, None) is not None
        and revisions[clip.current_revision_id].output_asset_id is not None
    )
    approved = sum(1 for clip in clips if clip.status == "approved")
    rejected = sum(1 for clip in clips if clip.status == "rejected")

    decided = unknown = failed = no_criteria = 0
    for revision in revisions.values():
        results = revision.acceptance_result_json or []
        if not results:
            no_criteria += 1
            continue
        for item in results:
            if item.get("result") == "unknown":
                unknown += 1
            else:
                decided += 1
                if item.get("result") == "fail":
                    failed += 1
    judged_total = decided + unknown
    unknown_ratio = (unknown / judged_total) if judged_total else 0.0

    budget = budget_status(session, episode.project_id)
    checks = [
        Check(
            "assets",
            "片段产物完整",
            bool(clips) and with_output == len(clips),
            {"clips": len(clips), "with_output": with_output},
        ),
        Check(
            "approval",
            "全部片段已通过审批",
            bool(clips) and approved == len(clips),
            {"clips": len(clips), "approved": approved, "rejected": rejected},
        ),
        Check(
            "quality",
            "质检无失败、未判率 ≤ 20%",
            failed == 0 and unknown_ratio <= UNKNOWN_RATIO_MAX,
            {
                "failed": failed,
                "unknown_ratio": round(unknown_ratio, 4),
                "no_criteria": no_criteria,
                "judged": judged_total,
            },
        ),
        Check(
            "budget",
            "项目当天预算未超",
            budget["tier"] != "over",
            {
                "tier": budget["tier"],
                "spent_today": str(budget["spent_today"]),
                "limit": str(budget["limit"]),
                "ratio": budget["ratio"],
            },
        ),
    ]
    blockers = [check.id for check in checks if not check.ok]
    return {
        "episode_id": episode.id,
        "status": episode.status,
        "ready": not blockers,
        "checks": [
            {"id": check.id, "label": check.label, "ok": check.ok, "detail": check.detail}
            for check in checks
        ],
        "blockers": blockers,
        "checked_at": datetime.now(UTC).replace(tzinfo=None),
    }
