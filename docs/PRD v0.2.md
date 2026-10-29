# PRD v0.2 — 漫剧工作台（求职 Demo · 可执行版）

> ⚠️ **已被 `PRD v0.3.md` 取代**（新增真实剧本基线 + 超管维护 Skill），本文件仅作历史留档。
> 状态：**范围冻结**（v0.2.1 就地修订：数据库定为 MySQL，见 ADR-006）。本文替代 `PRD v0.1.md`（草稿）作为唯一执行依据。
> 基线：6–8 周 / 2 人；AI 应用开发为主线、全栈为承载。
> 上游：`PRD v0.md`（愿望清单）→ `PRD v0.1.md`（裁剪评估）→ 本文（冻结）。
> 依据材料：`docs/Minimax Agent面试分享.md`、`docs/双非本拿下37k agent大厂offer要达到的强度.md`、`docs/经验教训.md`、`docs/video-motion-review/SKILL.md`、`docs/提示词/*`、`docs/gen_liaobots_img.py`。

---

## 0. 一页话

做一个**开源、零 key 可跑**的漫剧（AI 短剧）生产工作台：把一句原始想法，经「类型化改造 → 分集大纲 → 剧情 → 分镜 → 角色设定图 → 逐 clip 视频」推到成片，中间由一个 Agent 承担编排、提示词生成、失败归因与重试，用户只在**付费点**做确认。

它不是要替代商用漫剧平台，而是一个**能被追问 8 轮仍有料可讲**的技术作品集：每个功能都能对应到一个具体数字（成本、耗时、收敛次数、合格率）或一个具体失败案例。

---

## 1. 决策记录（本次 Q&A 结论，冻结）

| ADR | 决策 | 理由 | 代价 / 被否决方案 |
|---|---|---|---|
| **001** | 预算 **6–8 周 / 2 人**，范围按此冻结，任何新增功能必须**等量删除** | v0 全量 14–22 人周，超支 1.8×，且超支全在面试问不到处 | 承诺：超期不扩范围，先砍 P1 |
| **002** | 协作层 = **presence + 节点乐观锁 + 变更广播**；**不上 Yjs/CRDT**，进 roadmap | CRDT↔画布↔版本历史三方同步是分布式一致性问题；且"为什么不上 Yjs"是更好的面试答案 | 不能真·同时编辑同一字段（锁 10s 过期，冲突时后者重取）。否决：真 Yjs（要砍全能模式+版本链换工期） |
| **003** | 主界面 = **线性流水线**；画布降级为**镜头墙/看板**（纯 CSS grid，**不引 react-flow**） | 漫剧链路本质线性，DAG 自由度没有真实用户；省 8–12 人日与一个重依赖 | 失去"节点自由编排"观感。否决：无限画布+DAG |
| **004** | **Mock 为主 + 少量真实调用**（真实产出仅用于录制宣传素材） | 开源可信度（他人零 key 可跑）、演示不依赖网络、回归评测不烧钱 | 需维护 Mock 与真实产出的一致性 |
| **005** | 视频走**草稿/正片两级**（mini 480P → 720P+），Agent 默认模式**在付费点硬停** | 唯一直观的成本工程；`经验教训.md` #98 已记录火山涨价 0.08→0.20 元/秒 | 演示链路多一次确认交互（这正是要展示的点） |
| **006** | 数据库 = **MySQL 8.4**（InnoDB / utf8mb4）；不用 SQLite，不引 Redis | worker 是独立进程 + 2–5 人并发写，SQLite 单写者会成瓶颈；中小厂技术栈就是 MySQL，"按你们的栈做"比"我选了个更先进的但你们不用"更有说服力。MySQL 8 的 `FOR UPDATE SKIP LOCKED` + 行锁可直接替掉 Redis 队列 | 运维从 0 变成 1 个容器；DDL 非事务性，迁移需谨慎（见 §12） |

---

## 2. 范围冻结

### 2.1 P0 — 不做完不算完成

| # | 功能 | DoD（验收标准） |
|---|---|---|
| P0-1 | 数据模型（项目/集/clip/资产/版本/job/lesson/用户） | 迁移脚本可重复执行；`pytest` 覆盖级联删除 |
| P0-2 | 认证 + 三角色 6 权限 | 越权请求返回 403 且有测试；admin **不可写内容** |
| P0-3 | 流水线 UI（5 阶段 + clip 列表 + 镜头墙） | 新项目从空白到有分镜表，全程无手改数据库 |
| P0-4 | `LLMStep`：1 个步骤类型 + 5 份模板配置 + 结构化输出 + 校验重试 | 5 个环节共用一条代码路径；模板热改不需发版 |
| P0-5 | Agent 默认模式：跑链路 → 付费点停 → 逐 clip 确认/改/重发 | 拒绝一次后不产生任何计费；恢复后从断点继续 |
| P0-6 | 视频草稿生成 + `video-motion-review` 工具接入 | 工具返回结构化 verdict；flag 只提示、**不自动拒绝** |
| P0-7 | 成本台账（token / 秒数 / 单价 / 金额 / 耗时） | 任意一次生成可追到金额；按项目×模型聚合正确到分 |
| P0-8 | 提示词版本链 + 两版 diff + 回滚 | 回滚后重跑得到与目标版一致的请求参数 |
| P0-9 | 鲁棒性四件套（见 §5.4 矩阵） | 每类失败都有注入式测试（stub provider 抛错） |
| P0-10 | Mock / 回放 provider | **删掉 .env 直接 `docker compose up` 可跑完整演示** |

### 2.2 P1 — 有余量才做，按序

`P1-1` Agent 全能模式（单 clip、≤3 attempts、单任务预算上限、必停条件）
`P1-2` presence + 乐观锁 + 变更广播（SSE）
`P1-3` 可观测面板（每步 tool_call / token / 延迟 / 成本时间线）
`P1-4` 小评测集 20 条 + 回归脚本

### 2.3 非目标（写进 README，面试时是加分项）

不做：Yjs/CRDT、自由 DAG 编排引擎、节点 DSL/插件市场、二进制资产版本管理、多租户/计费/发票/审计、邮件通知与找回密码、Redis（缓存/限流/队列）、Celery/Kafka/k8s、MySQL 主从与分库分表、移动端/i18n/主题、模型自研与微调。

---

## 3. 架构与技术选型

### 3.1 分层（严格单向依赖）

```
apps/web (React)  ──HTTP/SSE──▶  apps/api (FastAPI 路由层：鉴权、权限、校验、序列化)
                                        │
                                   service 层（项目/生成/成本/协作 —— 拥有业务概念）
                                        │
                    ┌───────────────────┼────────────────────┐
              agent/（循环、组装、记忆）  providers/（适配层）   jobs/（队列、状态机、幂等）
                    └───────────────────┼────────────────────┘
                                   domain（纯模型 + 规则，无 IO）
                                        │
                                   infra（DB、对象存储、ffmpeg、日志）
```

硬规矩：`providers/` 不认识 `clip`/`project`，只认「一次调用 + 参数 + 预算」；`agent/` 不认识 HTTP；`apps/api` 不做业务计算。

### 3.2 选型表（**重点：为什么不用 X**）

| 层 | 选择 | 为什么不用更"企业级"的 |
|---|---|---|
| 后端 | FastAPI + SQLAlchemy | — |
| DB | **MySQL 8.4**（InnoDB，`utf8mb4_0900_ai_ci`）+ SQLAlchemy 2.0（`asyncmy`） | 不用 SQLite：worker 独立进程 + 多人并发写，单写者会成瓶颈；不用 Postgres：目标公司栈就是 MySQL。不做主从/读写分离/分库分表 |
| 队列 | **单进程 asyncio worker + `job` 表**，`SELECT … FOR UPDATE SKIP LOCKED` 领任务 | 不用 Celery/Redis：一天几十个生成，Broker 运维成本 > 收益。MySQL 8 行锁 + 幂等键已经解决"领任务不重复"，这也是二面"任务恢复"题的真答案 |
| 实时 | **SSE**（下行进度/成本/presence 广播）+ 普通 POST（心跳/加锁） | 不用 WebSocket：没有双向低延迟需求，SSE 少一层连接管理与重连逻辑 |
| 前端 | React + Vite + TS + TanStack Query + Tailwind | 不用 react-flow（画布已降级为看板）、不用 Next.js（无 SSR 需求）、不引状态管理库（Query 缓存够） |
| 认证 | JWT 放 httpOnly cookie，7 天，无 refresh | 不做 refresh 轮转：Demo 无长期会话需求，简单且可讲清 |
| 部署 | docker compose（api + web + worker + **mysql:8.4**），`init.sql` 建 utf8mb4 库，healthcheck + named volume，凭据用 `${VAR:-default}` 内联 | 不上 k8s、不引托管 DB、不要求对方先装 MySQL：一条 `compose up` 自带数据库 |

---

## 4. 数据模型

**MySQL 约定（一次定死，后面不返工）**

- **字符集 utf8mb4**：中文标题、台词、emoji 都要能存；建库即 `utf8mb4_0900_ai_ci`，不允许退回 `utf8mb3`。
- **时间一律 UTC + `DATETIME(3)`**：不用 `TIMESTAMP`（2038 上限 + 隐式随会话时区换算）。
- **`idem_key` 用 `CHAR(64)`**（sha256 hex）：定长唯一索引，避开长 VARCHAR 的索引长度上限。
- **`*_json` 用原生 `JSON` 列**：要按字段过滤时再加生成列 + 索引，不提前拆表。
- **索引**：`job(state, created_at)`（领任务扫描）、`revision(target_kind, target_id, created_at)`（版本链）、`revision(created_at)`（成本聚合）。
- **迁移**：Alembic；MySQL DDL 非事务性，故只允许"加表 / 加可空列 / 加索引"三类操作。

```
user(id, email, pwd_hash, role[user|lead|admin], created_at)

project(id, title, owner_id, genre, tone, status, created_at)
project_member(project_id, user_id, role[owner|member|viewer])

episode(id, project_id, idx, title, synopsis, status)
clip(id, episode_id, idx, duration_s, shot_type, camera, dialogue, prompt,
     ref_asset_ids_json, status[draft|generating|review|approved|rejected], current_revision_id)

asset(id, project_id, kind[image|video|audio], path, sha256, w, h, dur_s,
      source[gen|upload], revision_id, created_at)          -- 不存二进制，存路径+sha

revision(id, target_kind[clip|episode|project], target_id,
         prompt, negative, params_json, provider, model_requested, model_used,
         input_tokens, output_tokens, video_seconds, unit_price, cost, latency_ms,
         parent_revision_id, output_asset_id, created_at)     -- append-only，绝不 UPDATE

job(id, kind[text|image|video|review], target_kind, target_id, state, attempts, max_attempts,
    budget_limit, cost_used, idem_key[CHAR(64) UNIQUE], error_code, error_detail,
    created_at, started_at, finished_at)
    -- state: queued → running → waiting_confirm → done | failed | cancelled | paused_budget

lesson(id, scope, failure_type, symptom, fix, evidence_revision_id, hit_count, last_hit_at)
    -- scope 例: "project:{id}" / "genre:热血" / "model:seedance-mini"

presence(project_id, user_id, focus_target, last_seen_at)     -- 心跳表，不写入历史
lock_row(target_kind, target_id, user_id, expires_at)         -- 乐观锁，10s TTL
```

**为什么 `revision` 是 append-only**：它是成本审计 + 复盘 + 回滚的唯一真相源；允许 UPDATE 就等于放弃可复现性。

---

## 5. Agent 设计（本项目的技术核心）

### 5.1 上下文组装与预算（"上下文压缩"的答案）

单次组装上限 **12k input tokens**，按以下顺序**填满即停**；超限时按序裁剪：

| 序 | 内容 | 预算 | 裁剪优先级 |
|---|---|---|---|
| 1 | 系统提示词 + 输出 schema | 1.5k | 永不裁 |
| 2 | 项目卡（类型/基调/受众）+ 本集大纲 + 本集前序 clip 摘要 | 3k | 永不裁 |
| 3 | 人物卡（姓名/外形锚点/服装） | 3k | 先裁"详述"，只留 40 字锚点 |
| 4 | 上一集剧情摘要 | 1.5k | 再裁到 300 字 |
| 5 | 最近 3 条命中的 `lesson` | 2k | 3 → 1 条 |
| 6 | 当前 clip 的既有提示词 + 参考图指派 | 1k | 永不裁 |

> 关键点：**裁的是"长文本描述"，不是"结构性约束"**。人物外形锚点和参考图指派一旦丢，一致性立刻崩 —— 所以它们不在裁剪序列里。

### 5.2 Agent 循环（唯一控制流，默认/全能共用）

```python
async def run_clip(clip, mode):
    for attempt in range(1, MAX_ATTEMPTS + 1):              # 3
        if ctx.cost_used >= ctx.budget_limit:               # 预算熔断，先于一切
            return Stop("budget_exceeded", attempts=attempt)
        prompt = assemble(clip, lessons=recall(clip, k=3))
        if mode == "default":
            if not await gate_paid_confirm(preview=prompt):  # 付费点硬停
                return Paused(clip)                          # 不产生任何计费
        job = await jobs.submit("video", prompt=prompt, res=DRAFT,
                                idem_key=f"{clip.id}:{attempt}:{hash(prompt)}")
        out = await wait(job)
        report = await tools.video_motion_review(out.asset)  # vscan 扫描 + 视觉模型
        if report.verdict == "pass":
            return Ok(attempt, revision=out.revision_id)
        lesson = attribute(report)                            # 结构化归因
        if lesson is None or not lesson.actionable:           # 归因不出 → 不瞎重试
            return Escalate(report)
        save_lesson(lesson)                                   # 写记忆
        clip.prompt = rewrite(clip.prompt, lesson)             # 定向改写，不整体重写
    return Stop("max_attempts")
```

三条硬规矩：
1. **预算检查在循环最前面**，不在调用后 —— 否则最后一次超支无法挽回。
2. **归因不出来就不重试**。盲目重试是烧钱主因，也是面试官最爱追问的"你什么时候选择重新规划"。
3. **全能模式不是"去掉确认"**，是"把确认换成硬上限"：`≤3 attempts` + 单任务预算 + 必停条件三件套同时存在。

### 5.3 提示词生成（分工：模板 vs 模型）

- 模板负责**结构**：`{镜头时长} | {景别} | {运镜} | {画面主体+动作} | {台词} | {参考图: 角色A三视图}` —— §2 分镜时序、§4 运镜写法按 `Seedance 2.0 系列提示词指南`。
- 模型负责**内容**：把剧情句改写成画面描述，并做时长/字数配平（台词字数 ÷ 秒数校验，`经验教训.md` #94/#130 的教训：字数对不上会吞字漏字）。
- 结构不合法 → 不重试模型，**先用规则修**（缺字段补默认值、秒数超片长上限则拆 clip）。修不动才重试。
- 多参考图只做**指派**不做发明：参考图必须来自本项目 `asset`，不许 Agent 编造图像。

### 5.4 失败处理矩阵（每格都要有测试）

| 失败类型 | 检测 | 策略 | 上限 | 用户可见 |
|---|---|---|---|---|
| 输出非 JSON / schema 不符 | pydantic 校验 | 修 JSON 重试 1 次 → 换更强的降级 prompt 1 次 | 2 | 是（显示原始错误） |
| 结构残缺 | 规则校验 | **规则修补，不计重试** | — | 否 |
| 429 / 5xx | 状态码 | 指数退避 1s/2s/4s + 抖动 | 3 | 是（倒计时） |
| 超时 | 客户端 timeout | 重试 1 次；再超时降级到备用 model | 2 | 是 |
| 内容审核拒绝 | provider error code | **不重试**，改提示词（去敏感表述）后重提 | 1 | 是（原因） |
| 预算穿线 | 事前预算检查 | 立即停，转人工 | — | 是（已花/上限） |
| 额度/鉴权失败 | error code | 不重试，告警到 UI | — | 是 |
| 画面 flag（卡顿/静止） | vscan 运动曲线 | **不自动拒绝**，提示 + 建议 | — | 是 |
| 重复提交 | `idem_key` 冲突 | 返回既有 job，不重复计费 | — | 否 |

### 5.5 记忆（"记忆膨胀"的答案）

- **只写结构化记录**，不维护散文文档：`(scope, failure_type, symptom, fix, evidence_revision_id)`。
- **召回**：同 `scope` 或同 `genre`，`hit_count DESC` + 近 30 天，最多 3 条。
- **膨胀治理**：同 `(scope, failure_type, symptom)` 去重后 `hit_count += 1`；单 scope 超 200 条时把 `hit_count=1 且 last_hit_at > 90 天` 的归档（不删，留在 `revision` 里可追）。
- **可评估**：注入前后跑同一评测集，看「首次 attempt 通过率」是否上升 —— 记忆有没有用是可量化的，不是感觉。

---

## 6. API 契约（只列关键）

```
POST /auth/register|login|logout            GET /auth/me
GET  /projects                              POST /projects                (lead+)
GET  /projects/{id}                         PATCH /projects/{id}          (owner)
POST /projects/{id}/members                 (lead+)
GET  /projects/{id}/episodes                POST /episodes/{id}/clips
POST /steps/{episode_id}/run                # LLMStep，body: {template_id, params}
POST /clips/{id}/generate                   # body: {res: draft|final} → job
POST /jobs/{id}/confirm | cancel | retry
GET  /jobs/{id}                             GET /projects/{id}/events (SSE)
GET  /clips/{id}/revisions                  GET /revisions/{a}/diff/{b}
POST /clips/{id}/rollback                   # body: {revision_id}
GET  /projects/{id}/cost?group_by=model|stage|day
GET  /projects/{id}/lessons
POST /locks/acquire|release                 POST /presence/heartbeat
GET  /admin/providers                       PATCH /admin/providers/{name}
```

约定：错误体统一 `{code, message, detail, request_id}`；所有写接口需 `Idempotency-Key`；生成类接口一律返回 `job` 而非结果。

---

## 7. 成本模型（实测后填表，README 必带）

| 环节 | 模型/通道 | 单价 | 单次典型消耗 | 单次成本 |
|---|---|---|---|---|
| 文本（5 环节） | 待选（走 `text.plan` 路由） | 待实测 | ~8k in / 2k out | 待填 |
| 角色三视图/特写 | `gemini-3.1-flash-image-preview` | 待实测（按 `liaobots` 账单） | 1 图 | 待填 |
| 快速出图/图生图 | `gpt-image-2.5-flare` | 待实测 | 1 图 | 待填 |
| 精修 | `gpt-image-2.5-sunburst` | 待实测 | 1 图 | 待填 |
| 视频草稿 | Seedance mini 480P | **0.20 元/秒**（`经验教训.md` #98，火山 2026-09-18 涨价） | 5 秒 | **~1.0 元** |
| 视频正片 | Seedance 720P+ | 待实测 | 5 秒 | 待填 |

**演示预算**（写进 README，也是给面试官的定心丸）：一次完整演示 ≈ 8 个 clip 草稿（40 秒 ≈ 8 元）+ 一次全能模式重试（10 秒 ≈ 2 元）≈ **10 元/次**；全流程真实调用录制 6 次 ≈ **60 元**。Mock 通道 **0 元**。

---

## 8. 评测（P1-4，二面"怎么量化证明效果提升"的答案）

- **固定集**：20 条「原始想法 → 期望特征」，覆盖 5 种类型（言情/热血/悬疑/校园/职场），每条标注 3–5 个必含要素。
- **指标**（每次改提示词都跑）：

| 指标 | 定义 | 目标 |
|---|---|---|
| schema 合法率 | 一次通过校验的比例 | ≥95% |
| 要素覆盖 | 必含要素命中数 / 应含数 | ≥80% |
| 台词配平率 | `台词字数 ÷ 秒数` 落在 4–6 的 clip 占比 | ≥90% |
| 首次 attempt 通过率 | 视频质检 pass 且无需重写的比例 | ≥60% |
| 单集成本 / 耗时 | 台账取数 | 记录基线，不设目标 |
| 人工一致性分 | 抽 8 条按角色锚点是否漂移打 0–2 分 | ≥1.5 |

- 实现：`pytest -m eval`，Mock provider 下跑（零成本），LLM-as-judge + 人工抽检 8 条校准。

---

## 9. 里程碑（6 周 / 2 人：A=后端+Agent，B=前端+UI+集成）

| 周 | A | B | 周末 DoD |
|---|---|---|---|
| W1 | 骨架、8 张表 + Alembic 迁移、compose（含 mysql:8.4）、认证、权限矩阵、MockProvider | 流水线空壳、项目/集/clip 三栏、Query 层 | 零 key `compose up` 起得来（空 MySQL 自动建表），越权测试通过 |
| W2 | `LLMStep`（1 路径 5 模板）、结构化输出、校验重试、`revision` 链 | 步骤表单、分镜表编辑器、版本 diff/回滚 UI | 想法 → 分镜表全链路，diff 可用 |
| W3 | 图像 provider 适配 + 三视图/特写/精修预设 + 资产库 | 资产库、参考图指派交互、镜头墙 v1 | 角色设定成图，指派进 clip |
| W4 | job 队列/状态机/幂等、预算熔断、成本台账、SSE | job 进度条、付费点确认弹窗、成本看板 | 出第一条真实草稿 clip，账对到分 |
| W5 | `video-motion-review` 工具包装、失败归因、全能模式（≤3 次） | 质检结果卡（时间码可点）、归因展示、重试 UI | 演示「错了 → 诊断 → 定向改写 → 重发」 |
| W6 | presence/乐观锁、可观测面板、评测集 + 回归 | 协作指示器、可观测面板 UI、README/架构图/3 分钟录屏 | 逐条过 §11 验收清单 |

> 里程碑纪律：**每周五只判"DoD 是否可演示"**，不判"完成度百分比"。第 3 周结束若图像链路未通，立刻砍 P1-1 全能模式保住 P0。

---

## 10. 3 分钟演示脚本（面试现场按这个走）

| 时间 | 做什么 | 说什么 |
|---|---|---|
| 0:00–0:20 | 打开项目，「言情改热血」一条想法 | 一句话定位：不是画布工具，是**受控的 Agent 生产链路** |
| 0:20–0:50 | 跑 5 个 LLMStep → 分镜表 | 5 个环节一条代码路径，模板热改不发版；这里讲上下文预算怎么裁 |
| 0:50–1:20 | 角色三视图 → 参考图指派 | 一致性靠"锚点 + 指派"，不靠模型记忆 |
| 1:20–1:50 | 点生草稿 → **付费点确认弹窗** | 成本台账：这一条 ~1 元，先便宜后贵 |
| 1:50–2:30 | 质检 flag → 归因 → 定向改写 → 重发 | 全能模式 ≤3 次、预算熔断、归因不出就不重试 |
| 2:30–2:50 | 版本 diff + 回滚 + 成本看板 | 可复现、可审计、可量化 |
| 2:50–3:00 | 打开 README 的「设计取舍」一节 | 主动交底：为什么不上 Yjs / 不用 Redis（MySQL 行锁够用）/ 为什么 MySQL 而不是 SQLite |

---

## 11. 验收清单（W6 周五逐条打勾）

- [ ] 删掉 `.env`，`docker compose up` → 完整演示可跑（Mock + 自带 MySQL）
- [ ] MySQL 8.4 空实例冷启动能自动建库建表；Alembic 全量迁移在空库上可重复执行
- [ ] 中文 / emoji 在标题、台词、教案往返无损（utf8mb4 验证）
- [ ] 起 2 个 worker 抢同一批 job，无重复执行、无重复计费（`SKIP LOCKED` 验证）
- [ ] 三类角色越权测试全绿；admin 无法写内容
- [ ] 一次生成可追到具体金额，聚合正确到分
- [ ] 拒绝付费点后账户零计费、job 状态为 `cancelled`
- [ ] 每类失败（§5.4）有注入式测试，UI 有对应文案
- [ ] 同 `idem_key` 重复提交不双扣费
- [ ] 提示词回滚后重跑，请求参数与目标版一致
- [ ] `pytest -m eval` 在 Mock 下 ≤2 分钟跑完并输出指标表
- [ ] README 含：架构图、3 分钟录屏、实测成本表、**5 条设计取舍**、零 key 启动、评测结果、roadmap
- [ ] 开源卫生：无 key/无个人路径、`.env.example`、LICENSE、素材来源与版权声明

---

## 12. 风险登记

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 被追问"协作层为什么这么弱" | 中 | 中 | README 已有 ADR-002 文本；正面答取舍 + roadmap，比半成品 CRDT 强 |
| 真实 API 涨价/限流（#98 已涨 2.5×） | 高 | 中 | 单一 `providers` 层 + 价格表外置；Mock 保障演示不断 |
| Seedance 并发上限、长片段时间上限（单片 ≤15s） | 中 | 高 | 分镜阶段就按 ≤15s 切分；队列串行不并发（也省钱） |
| 一致性差（角色漂移） | 高 | 中 | 锚点 + 参考图指派 + 人工一致性分指标；不承诺"自动一致" |
| 素材版权 | 中 | 高 | 生成素材标注来源模型；仓库不收录真人肖像；LICENSE 写清 |
| 6 周做不完 | 中 | 高 | 每周五 DoD 判据 + 预设砍单顺序：P1-4 → P1-3 → P1-1 → P1-2 |
| Agent 失控烧钱 | 低 | 高 | 预算熔断前置 + `budget_limit` 落库 + 默认模式付费点硬停 |
| MySQL 容器拖慢首次启动 / 面试官机器没 Docker | 中 | 中 | healthcheck + 预热卷；同时提供 `scripts/dev.sh`（本机已有 MySQL 时直连），compose 只是默认路径 |
| MySQL DDL 非事务性，迁移失败留半成品 | 中 | 中 | 迁移只允许加表/加可空列/加索引；迁移前 `mysqldump` 快照；CI 对空库跑一遍全量迁移 |
| 连接池被 SSE 长连接耗尽 | 低 | 中 | SSE 不持 DB 连接（事件从内存队列出）；`pool_size` 显式配置 + `pool_pre_ping` |

---

## 13. 变更记录

| 版本 | 变化 |
|---|---|
| v0 | 功能愿望清单：无限画布 + 5 类文本节点 + Yjs + 资产/节点版本 + 完整三角色 + 多 provider 网关 + 双模式 Agent |
| v0.1 | 逐条评估，标出 10 项过度设计；给 4 条判定标准与工作量账（14–22 人周 vs 预算 6–8 周）；提出 4 个待确认问题 |
| **v0.2** | **冻结**：ADR-001~005 落定；范围收敛为 10 项 P0 + 4 项 P1；补 Agent 上下文预算、失败矩阵、记忆治理、成本模型、评测指标、6 周分工、演示脚本、验收清单、风险登记 |
| **v0.2.1** | 就地修订：数据库定为 **MySQL 8.4**（ADR-006）。补三项连带影响：① 队列改用 `FOR UPDATE SKIP LOCKED` 领任务（Redis 正式进非目标）；② §4 增补 utf8mb4 / `DATETIME(3)` / `CHAR(64)` 索引 / JSON 列 / 迁移纪律五条约定；③ compose 纳入 mysql 服务、验收清单加迁移与并发抢单、风险登记加 DDL 与连接池两条 |

**下一版（v0.3，W2 末再写）**：把模板配置、`providers` 适配层接口、SSE 事件格式写成正式契约，并填入实测成本数字。
