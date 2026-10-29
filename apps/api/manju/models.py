"""13 张表的 ORM 定义，逐字段对应 `docs/开发文档/01-数据结构.md` §4。

表名/索引名/约束名与文档完全一致 —— 迁移、文档、模型三者对不上时以文档为准。
MySQL 专属细节（unsigned、DATETIME(3)、生成列、utf8mb4 排序规则）都收在这个文件里，
换库要重写这里，业务代码不感知。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

BIGINT = mysql.BIGINT(unsigned=True)
INT_U = mysql.INTEGER(unsigned=True)
TINY_U = mysql.TINYINT(unsigned=True)
BOOL = mysql.TINYINT(display_width=1)
DT3 = mysql.DATETIME(fsp=3)
MEDIUM = mysql.MEDIUMTEXT()
MONEY = Numeric(12, 6)

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(190), nullable=False)
    pwd_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="user")
    is_active: Mapped[int] = mapped_column(BOOL, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (
        UniqueConstraint("email", name="uk_user_email"),
        CheckConstraint("role IN ('user','lead','admin')", name="ck_user_role"),
        TABLE_KW,
    )


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("user.id", name="fk_project_owner"), nullable=False
    )
    genre: Mapped[str | None] = mapped_column(String(24))
    tone: Mapped[str | None] = mapped_column(String(120))
    logline: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    version: Mapped[int] = mapped_column(INT_U, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}

    __table_args__ = (
        Index("idx_project_owner", "owner_id"),
        CheckConstraint("status IN ('active','archived')", name="ck_project_status"),
        TABLE_KW,
    )


class ProjectMember(Base):
    __tablename__ = "project_member"

    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("project.id", name="fk_pm_project", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("user.id", name="fk_pm_user", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="member")
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (
        Index("idx_pm_user", "user_id"),
        CheckConstraint("role IN ('member','viewer')", name="ck_pm_role"),
        TABLE_KW,
    )


class Episode(Base):
    __tablename__ = "episode"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("project.id", name="fk_ep_project", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(INT_U, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    synopsis: Mapped[str | None] = mapped_column(MEDIUM)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    version: Mapped[int] = mapped_column(INT_U, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}

    __table_args__ = (
        UniqueConstraint("project_id", "idx", name="uk_ep_idx"),
        CheckConstraint("status IN ('draft','scripted','storyboarded','producing','done')", name="ck_ep_status"),
        CheckConstraint("idx BETWEEN 1 AND 50", name="ck_ep_idx"),
        TABLE_KW,
    )


class Clip(Base):
    __tablename__ = "clip"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("episode.id", name="fk_clip_ep", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(INT_U, nullable=False)
    source_idx: Mapped[int] = mapped_column(INT_U, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    duration_s: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    prompt: Mapped[str | None] = mapped_column(MEDIUM)
    negative: Mapped[str | None] = mapped_column(Text)
    ref_slots_json: Mapped[dict | None] = mapped_column(JSON)
    shots_json: Mapped[dict | None] = mapped_column(JSON)
    dialogue_json: Mapped[dict | None] = mapped_column(JSON)
    acceptance_json: Mapped[dict | None] = mapped_column(JSON)
    current_revision_id: Mapped[int | None] = mapped_column(BIGINT)
    parent_clip_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("clip.id", name="fk_clip_parent", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(INT_U, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DT3, nullable=False)
    # 必须 VIRTUAL 不能 STORED：MySQL 禁止「STORED 生成列的基列上带 ON DELETE SET NULL 的外键」，
    # 而 parent_clip_id 正是 SET NULL。VIRTUAL 列同样可以建唯一二级索引。
    idx_key: Mapped[int | None] = mapped_column(
        INT_U, Computed("IF(parent_clip_id IS NULL, idx, NULL)", persisted=False)
    )

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}

    __table_args__ = (
        Index("uk_clip_ep_idx", "episode_id", "idx_key", unique=True),
        Index("idx_clip_status", "episode_id", "status", "id"),
        Index("idx_clip_parent", "parent_clip_id"),
        Index("idx_clip_rev", "current_revision_id"),
        CheckConstraint(
            "status IN ('draft','prompted','generating','review','approved','rejected')", name="ck_clip_status"
        ),
        CheckConstraint("duration_s BETWEEN 1.00 AND 15.00", name="ck_clip_dur"),
        CheckConstraint("idx BETWEEN 1 AND 30", name="ck_clip_idx"),
        TABLE_KW,
    )


class Asset(Base):
    __tablename__ = "asset"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("project.id", name="fk_asset_project", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    thumb_path: Mapped[str | None] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(mysql.CHAR(64), nullable=False)
    bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    w: Mapped[int | None] = mapped_column(INT_U)
    h: Mapped[int | None] = mapped_column(INT_U)
    thumb_w: Mapped[int | None] = mapped_column(INT_U)
    thumb_h: Mapped[int | None] = mapped_column(INT_U)
    dur_s: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    revision_id: Mapped[int | None] = mapped_column(BIGINT)
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "sha256", name="uk_asset_sha"),
        Index("idx_asset_list", "project_id", "kind", "id"),
        Index("idx_asset_rev", "revision_id"),
        CheckConstraint("kind IN ('image','video','audio')", name="ck_asset_kind"),
        CheckConstraint("source IN ('gen','upload','seed')", name="ck_asset_source"),
        TABLE_KW,
    )


class Revision(Base):
    __tablename__ = "revision"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    prompt: Mapped[str | None] = mapped_column(MEDIUM)
    negative: Mapped[str | None] = mapped_column(Text)
    params_json: Mapped[dict | None] = mapped_column(JSON)
    provider: Mapped[str | None] = mapped_column(String(32))
    model_requested: Mapped[str | None] = mapped_column(String(64))
    model_used: Mapped[str | None] = mapped_column(String(64))
    skill_versions_json: Mapped[dict | None] = mapped_column(JSON)
    input_tokens: Mapped[int | None] = mapped_column(INT_U)
    output_tokens: Mapped[int | None] = mapped_column(INT_U)
    video_seconds: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(INT_U)
    motion_json: Mapped[dict | None] = mapped_column(JSON)
    acceptance_result_json: Mapped[dict | None] = mapped_column(JSON)
    parent_revision_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("revision.id", name="fk_rev_parent", ondelete="SET NULL")
    )
    output_asset_id: Mapped[int | None] = mapped_column(BIGINT)
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (
        Index("idx_rev_target", "target_kind", "target_id", "id"),
        Index("idx_rev_cost", "created_at"),
        Index("idx_rev_asset", "output_asset_id"),
        Index("idx_rev_parent", "parent_revision_id"),
        CheckConstraint("target_kind IN ('clip','episode','project','skill')", name="ck_rev_kind"),
        CheckConstraint("cost >= 0", name="ck_rev_cost"),
        TABLE_KW,
    )


class Job(Base):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(TINY_U, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(TINY_U, nullable=False, server_default="3")
    budget_limit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cost_used: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    idem_key: Mapped[str] = mapped_column(mysql.CHAR(64), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="default")
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    locked_by: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DT3)
    error_code: Mapped[str | None] = mapped_column(String(48))
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DT3)
    finished_at: Mapped[datetime | None] = mapped_column(DT3)

    __table_args__ = (
        UniqueConstraint("idem_key", name="uk_job_idem"),
        Index("idx_job_claim", "state", "created_at"),
        Index("idx_job_target", "target_kind", "target_id", "id"),
        CheckConstraint("kind IN ('text','image','video','review')", name="ck_job_kind"),
        CheckConstraint(
            "state IN ('queued','running','waiting_confirm','done','failed','cancelled','paused_budget')",
            name="ck_job_state",
        ),
        CheckConstraint("attempts <= max_attempts", name="ck_job_attempt"),
        CheckConstraint("cost_used >= 0 AND budget_limit > 0", name="ck_job_budget"),
        TABLE_KW,
    )


class JobTrace(Base):
    """job 的执行时间线（`02` §5.4 的 trace，PRD P1-4）。

    只留最近 `TRACE_KEEP` 条：它的用途是在 job 详情页回答"这一步发生了什么、花了多久"，
    不是审计日志 —— 钱与产物看 `revision`，终态看 `job`。
    """

    __tablename__ = "job_trace"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("job.id", name="fk_trace_job", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DT3, nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict: Mapped[str | None] = mapped_column(String(16))
    ms: Mapped[int | None] = mapped_column(INT_U)
    detail: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (
        Index("idx_trace_job", "job_id", "id"),
        TABLE_KW,
    )


class Lesson(Base):
    __tablename__ = "lesson"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(96), nullable=False)
    failure_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symptom: Mapped[str] = mapped_column(String(500), nullable=False)
    symptom_key: Mapped[str] = mapped_column(mysql.CHAR(64), nullable=False)
    fix: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_revision_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("revision.id", name="fk_lesson_rev", ondelete="SET NULL")
    )
    hit_count: Mapped[int] = mapped_column(INT_U, nullable=False, server_default="1")
    last_hit_at: Mapped[datetime] = mapped_column(DT3, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DT3)
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (
        UniqueConstraint("scope", "failure_type", "symptom_key", name="uk_lesson"),
        Index("idx_lesson_recall", "scope", "archived_at", "hit_count"),
        TABLE_KW,
    )


class Skill(Base):
    __tablename__ = "skill"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", name="uk_skill_name"),
        CheckConstraint("stage IN ('S1','S2','S3','S4','S5','review')", name="ck_skill_stage"),
        CheckConstraint("status IN ('active','disabled')", name="ck_skill_status"),
        TABLE_KW,
    )


class SkillVersion(Base):
    __tablename__ = "skill_version"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    skill_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("skill.id", name="fk_sv_skill", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(INT_U, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    body_md: Mapped[str] = mapped_column(MEDIUM, nullable=False)
    sections_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    tool_ref: Mapped[str | None] = mapped_column(String(64))
    checksum: Mapped[str] = mapped_column(mysql.CHAR(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("user.id", name="fk_sv_user"), nullable=False
    )
    dry_run_ok_at: Mapped[datetime | None] = mapped_column(DT3)
    superseded_at: Mapped[datetime | None] = mapped_column(DT3)
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)
    active_flag: Mapped[int | None] = mapped_column(
        mysql.TINYINT, Computed("IF(state = 'active', 1, NULL)", persisted=True)
    )

    __table_args__ = (
        UniqueConstraint("skill_id", "version", name="uk_sv_ver"),
        Index("uk_sv_active", "skill_id", "active_flag", unique=True),
        Index("idx_sv_list", "skill_id", "id"),
        CheckConstraint("state IN ('draft','active','superseded')", name="ck_sv_state"),
        TABLE_KW,
    )


class Presence(Base):
    __tablename__ = "presence"

    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("project.id", name="fk_pres_project", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("user.id", name="fk_pres_user", ondelete="CASCADE"), primary_key=True
    )
    focus_target: Mapped[str | None] = mapped_column(String(40))
    last_seen_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (Index("idx_presence_seen", "last_seen_at"), TABLE_KW)


class LockRow(Base):
    __tablename__ = "lock_row"

    target_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_id: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("user.id", name="fk_lock_user", ondelete="CASCADE"), nullable=False
    )
    acquired_at: Mapped[datetime] = mapped_column(DT3, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (
        Index("idx_lock_exp", "expires_at"),
        CheckConstraint("target_kind IN ('clip','episode','skill_version')", name="ck_lock_kind"),
        TABLE_KW,
    )


class RuntimeFlag(Base):
    """运行时开关：环境变量决定"接的是什么"，这张表决定"**现在能不能用**"。

    与 `Provider` 的关系是"配置 vs 运行态"：key 配在环境变量里、进程内不可改；
    而"某条通道临时停用"是可变的运行状态，必须落在共享存储里 —— 否则多副本时各实例各说各话。
    """

    __tablename__ = "runtime_flag"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="provider")
    key_name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[int] = mapped_column(BOOL, nullable=False, server_default="1")
    reason: Mapped[str | None] = mapped_column(String(255))
    actor_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("user.id", name="fk_flag_user", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (
        UniqueConstraint("kind", "key_name", name="uk_flag"),
        CheckConstraint("kind IN ('provider')", name="ck_flag_kind"),
        TABLE_KW,
    )


class EventLog(Base):
    """项目级事件日志：SSE 的**跨副本**权威来源（`02` §7.4）。

    进程内总线负责低延迟直推；这张表负责"别的副本发的事件，本副本也能补上"。
    `id` 就是 SSE 的 `Last-Event-ID`：全局自增，任意副本都能按它续传。
    """

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    # 故意**不加 FK**：`publish` 常常发生在已持有 `project` 行锁的事务里（submit / confirm），
    # 而 FK 校验要求父行的 S 锁 —— 会等自己那把 X 锁，直到 lock wait timeout（实测 30 s，
    # 且异常被吞后表现为"事件没落库、接口变慢"）。日志是投递通道不是账本，孤儿行由 prune 清。
    project_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DT3, nullable=False)

    __table_args__ = (
        Index("idx_event_project_seq", "project_id", "id"),
        TABLE_KW,
    )
