# PRD v0.3 — 漫剧工作台（求职 Demo · 可执行版）

> 状态：**范围冻结（v0.3.1 修订）**。替代 `PRD v0.2.md`。
> 本版两项变更：① 演示与评测数据**不现编**，全部来自真实项目 `docs/我师父是个普通人/`；② 超管新增**维护 Skill** 职责。
> v0.3.1：素材确认走 **GitHub Release 分发**，仓库零二进制（见 §3.3）。
> 实现契约见 **`docs/开发文档/`**：`00-约定与索引` / `01-数据结构` / `02-API协议` / `03-异常包容与边界值`。
> 依据：`docs/Minimax Agent面试分享.md`、`docs/双非本拿下37k agent大厂offer要达到的强度.md`、`docs/我师父是个普通人/`（剧本/剧情/终剪提示词/人物卡）、`docs/经验教训.md`、`docs/video-motion-review/SKILL.md`、`docs/提示词/*`、`docs/gen_liaobots_img.py`。

---

## 0. 一页话

开源、零 key 可跑（lite 素材包 ~25 MB）的漫剧（AI 短剧）生产工作台：把一句原始想法经「类型化改造 → 分集剧情 → 分集剧本分镜 → 终剪提示词 → 人物卡 → 逐 clip 视频」推到成片，中间由 Agent 承担编排、提示词生成、失败归因与重试，用户只在**付费点**做确认。

演示数据、模板、Mock fixture、评测 golden **全部复用已完成的真实项目《师尊别砍我系统》第一集**，不虚构。Skill（提示词规则包）由超管版本化维护，用户只读。

---

## 1. 决策记录

| ADR | 决策 | 理由 | 代价 / 被否决方案 |
|---|---|---|---|
| **001** | 预算 **6–8 周 / 2 人**，范围按此冻结，新增功能必须等量删除 | v0 全量 14–22 人周，超支 1.8×，且超支全在面试问不到处 | 超期不扩范围，先砍 P1 |
| **002** | 协作层 = **presence + 节点乐观锁 + 变更广播**；不上 Yjs/CRDT，进 roadmap | CRDT↔画布↔版本历史三方同步是分布式一致性问题；"为什么不上"是更好的面试答案 | 不能真·同时编辑同一字段（锁 10s 过期） |
| **003** | 主界面 = **线性流水线**；画布降级为**镜头墙看板**（纯 CSS grid，不引 react-flow） | 漫剧链路本质线性，DAG 自由度没有真实用户 | 失去"自由编排"观感 |
| **004** | **Mock 为主 + 少量真实调用**；Mock 的输入输出**直接取真实第一集产物** | 开源可信度、演示不依赖网络、回归不烧钱；真实数据让 Mock 不是假的 | 需维护 fixture 与真实产出的口径一致 |
| **005** | 视频**草稿/正片两级**（mini 480P → 720P+），默认模式在**付费点硬停** | 唯一直观的成本工程；实测 12s ≈ 2.27 元 | 演示多一次确认交互（正是要展示的点） |
| **006** | 数据库 = **MySQL 8.4**（InnoDB/utf8mb4）；不用 SQLite，不引 Redis | worker 独立进程 + 多人并发写；中小厂栈就是 MySQL；`FOR UPDATE SKIP LOCKED` 可替掉 Redis 队列 | 运维从 0 变 1 个容器；DDL 非事务性 |
| **007** | **唯一演示数据源 = 真实第一集**，不另造剧本 | "项目真实性"是两份面试文档的核心考点；手里已有 7 个成片、60 余张设定图、19 个替换件，现编等于自废优势 | 演示与仓库绑定真实作品，素材不能进 Git（见 §12） |
| **008** | **Skill 归超管维护**，且 admin 对**项目内容仍只读** | Skill 是全局知识层，改动影响所有项目，属于配置边界；内容仍是创作者所有。这也正是"Skill 模块怎么设计"的原题 | 权限矩阵从 6 条变 7 条，且出现"最高权限不能改内容"的非对称 |

---

## 2. 范围冻结

### 2.1 P0 — 不做完不算完成

| # | 功能 | DoD |
|---|---|---|
| P0-1 | 数据模型（项目/集/clip/资产/版本/job/lesson/用户） | 迁移脚本可重复执行；`pytest` 覆盖级联删除 |
| P0-2 | 认证 + 三角色 7 权限 | 越权返回 403 且有用例；admin 不可写内容 |
| P0-3 | 流水线 UI（5 阶段 + clip 列表 + 镜头墙） | 新项目从空白到有分镜表，全程无手改数据库 |
| P0-4 | `LLMStep`：1 个步骤类型 + 5 份**从真实产物反推**的模板 | 5 环节共用一条代码路径；模板热改不发版 |
| P0-5 | Agent 默认模式：跑链路 → 付费点停 → 逐 clip 确认/改/重发 | 拒绝后零计费；恢复后从断点继续 |
| P0-6 | 视频草稿 + `video-motion-review` 工具 + 逐条验收点判定 | 返回结构化 verdict + 动线指标；flag 只提示不自动拒绝 |
| P0-7 | 成本台账（token/秒数/单价/金额/耗时） | 任一生成可追到金额；聚合正确到分 |
| P0-8 | 提示词版本链 + 两版 diff + 回滚 | 回滚后重跑参数与目标版一致 |
| P0-9 | 鲁棒性四件套（§6.4 矩阵） | 每类失败有注入式测试 |
| P0-10 | Mock / 回放 provider（fixture 取自真实第一集） | 删掉 `.env` → `compose up` 可跑完整演示 |

### 2.2 P1 — 按序，做完一条再做下一条

`P1-1` **Skill 维护（超管）**：列表 / 编辑存为新版 / 两版 diff / 试跑 dry-run / 发布与停用 — 3 人日
`P1-2` Agent 全能模式（单 clip、≤3 attempts、单任务预算、必停条件）— 3 人日
`P1-3` presence + 乐观锁 + 变更广播（SSE）— 3 人日
`P1-4` 精简 trace（job 详情页内的时间线，不做独立面板）+ 评测集 20 条 — 1.5 人日

### 2.3 非目标

不做：Yjs/CRDT、自由 DAG 编排、节点 DSL/插件市场、二进制资产版本管理、多租户/计费/发票/审计、邮件与找回密码、Redis、Celery/Kafka/k8s、MySQL 主从与分库分表、移动端/i18n/主题、模型自研与微调、**Skill 内可执行脚本上传**（见 §7.5）。

### 2.4 为 Skill 让路的削减（账要摊开）

| 削减 | 省 |
|---|---|
| 可观测独立面板 → 收敛为 job 详情页内 trace | 1.5 人日 |
| 图像预设从 4 个减到 3 个（三视图/立绘、面部特写、精修；"快速出图"归入立绘预设） | 0.5 人日 |
| W1 骨架里不做资产上传（只做生成资产 + 种子导入） | 1.0 人日 |
| **Skill 维护净新增** | **+3 人日** |

净增 0，6 周预算不变。

---

## 3. 真实剧本基线（新）

### 3.1 手里已有的东西

`docs/我师父是个普通人/` — 剧名《师尊别砍我系统》，**134 文件 / 566 MB**：文本 13 个 md 约 330 KB，PNG 355 MB（人物三视图/立绘/面部特写/场景/道具），MP4 171 MB（19 个，含 `s3b_`、`s6a_` 这类**片段级替换件**）。

| 真实产物 | 体量 | 在系统里对应 |
|---|---|---|
| `人物设定.md` | 185 B | S1 人物锚点（女主/师父/系统三行） |
| `第一集剧情.md` … `第五集剧情.md` | 4.6–20.9 KB | S2 分集剧情 |
| `第一集\剧本.md`、`第二集\剧本.md` | 16 KB / 32.6 KB | S3 分集剧本分镜（含 §1 制作规范、Clip 拆解、镜头时序、台词、运镜、参考资产规划） |
| `第一集\Seedance2.0终剪提示词.md` | **168 KB** | S4 终剪提示词（逐片段：核心剧情/参考图/**槽位 `ref_image_0..N`**/提示词正文/台词/注意 ⑩ 条/实测经验/验收点 ✅❌） |
| `第二集\…终剪提示词.md` | 64 KB | 同上 |
| `人物卡\人物卡提示词.md`、`概念设定案提示词.md`、`Krea2人物卡提示词.md` | 8.5–16 KB | S5 人物卡 |
| `第一集\s1..s7*.mp4`（8 个）、`第二集`（11 个） | 171 MB | 成片 + 替换件 |

### 3.2 三处用途（这条 ADR 的实际收益）

1. **模板来源**：P0-4 的 5 份模板不靠想象，直接按上表每个真实产物的结构反推字段。
2. **Mock fixture**：`MockProvider` 的返回值取真实产物 —— 12.04 s / 1280×720 / 11 MB / `usage.total_tokens=260,100`。演示手感与真实一致，且零成本。
3. **评测 golden**：第一集的 7 个 clip 分镜就是标准答案，改提示词/改 Skill 后跑回归（§9）。

### 3.3 素材获取：仓库零二进制，Release 分发

仓库**不收任何二进制**（只有 330 KB 文本）。理由不是"仓库太大不好看"，而是硬约束：GitHub 单文件 >100 MB 直接拒推，566 MB 的仓库会让 clone / CI / fork 全部变慢，而且**一旦进了历史就很难干净移除**。

`scripts/fetch_demo_assets.py` 三档：

| 档 | 内容 | 体积 | 用途 |
|---|---|---|---|
| `--mock-lite`（默认） | 12 张缩略图（长边 ≤720px）+ 3 个 480p 短片段 + 全部文本 | ~25 MB | **零 key 跑通完整演示**（Mock 通道） |
| `--full` | 第一集 8 + 第二集 11 个成片、83 张 PNG 原图 | ~566 MB | 真实调用录制、一致性对比 |
| `--local <path>` | 指向自己已有的同名目录 | 0 | 作者本人 / 自带素材 |

- 分发：**GitHub Release 分卷 3 × ~190 MB**（单文件上限 2 GiB，不分卷也可，但分卷能断点续传，国内更稳）+ **网盘镜像**（README 给链接，国内直连 Release 常不可用）。
- 每卷 `sha256` 写进 `assets.lock.json`，脚本校验后才解压；`.gitignore` 锁 `docs/我师父是个普通人/` 与 `demo/assets/`。
- `scripts/seed_project.py` 把 `人物设定.md + 第N集剧情.md + 第N集\剧本.md + 终剪提示词.md` 解析成 `project / episode / clip / shots / acceptance` 灌库；**解析失败报行号，不许静默跳过** —— 这个脚本本身就是"结构化输出"的展示面。

---

## 4. 架构与技术选型

### 4.1 分层（严格单向）

```
apps/web (React) ──HTTP/SSE──▶ apps/api（鉴权、权限、校验、序列化）
                                     │
                                service（项目/生成/成本/协作/Skill —— 拥有业务概念）
                                     │
        ┌────────────────────────────┼──────────────────────────┐
   agent（循环、组装、记忆、Skill 召回）  providers（适配层）      jobs（队列、状态机、幂等）
        └────────────────────────────┼──────────────────────────┘
                                domain（纯模型 + 规则，无 IO）
                                     │
                                infra（DB、对象存储、ffmpeg、日志）
```

硬规矩：`providers/` 不认识 `clip`/`project`；`agent/` 不认识 HTTP；`apps/api` 不做业务计算；`Skill` 的存储归 service，**规则的选择逻辑归 agent**。

### 4.2 选型表

| 层 | 选择 | 为什么不用更"企业级"的 |
|---|---|---|
| 后端 | FastAPI + SQLAlchemy 2.0 | — |
| DB | **MySQL 8.4**（InnoDB，`utf8mb4_0900_ai_ci`，`asyncmy`） | 不用 SQLite：worker 独立进程 + 并发写，单写者成瓶颈；不用 Postgres：目标公司栈就是 MySQL。不做主从/读写分离/分库分表 |
| 队列 | 单进程 asyncio worker + `job` 表，`SELECT … FOR UPDATE SKIP LOCKED` 领任务 | 不用 Celery/Redis：一天几十个生成，Broker 运维成本 > 收益；MySQL 8 行锁 + 幂等键已解决"领任务不重复" |
| 实时 | SSE 下行 + POST 上行（心跳/加锁） | 不用 WebSocket：无双向低延迟需求 |
| 前端 | React + Vite + TS + TanStack Query + Tailwind | 不用 react-flow（画布已降级）、不用 Next.js（无 SSR）、不引状态库 |
| 认证 | JWT 放 httpOnly cookie，7 天，无 refresh | 不做轮转：Demo 无长期会话需求 |
| 部署 | docker compose（api + web + worker + **mysql:8.4**），`init.sql` 建 utf8mb4 库，healthcheck + volume，凭据 `${VAR:-default}` 内联 | 不上 k8s、不引托管 DB、不要求对方先装 MySQL |

---

## 5. 数据模型

**MySQL 约定**：utf8mb4 建库；时间一律 UTC + `DATETIME(3)`（不用 `TIMESTAMP`，2038 上限 + 隐式时区换算）；`idem_key CHAR(64)`；`*_json` 用原生 `JSON` 列；索引 `job(state, created_at)`、`revision(target_kind, target_id, created_at)`、`revision(created_at)`、`skill_version(skill_id, created_at)`；Alembic 只允许「加表 / 加可空列 / 加索引」。

```
user(id, email, pwd_hash, role[user|lead|admin], created_at)

project(id, title, owner_id, genre, tone, status, created_at)
project_member(project_id, user_id, role[owner|member|viewer])

episode(id, project_id, idx, title, synopsis, status)
clip(id, episode_id, idx, duration_s, status, current_revision_id,
     ref_slots_json,      -- [{idx:1, asset_id, role:"实景"|"造型锚"|"构图锚"|"形态参考"}]
     shots_json,          -- [{no:1, start:0.0, end:3.5, desc, camera}]  加总必须 == duration_s
     dialogue_json,       -- [{speaker, text, shot_no, on_screen:false}]  台词原文内嵌，非摘要
     acceptance_json,     -- [{id, criterion, result:null|pass|fail, evidence}]
     parent_clip_id)      -- 替换件：重跑不覆盖原片，挂到原 clip 下

asset(id, project_id, kind[image|video|audio], path, sha256, w, h, dur_s,
      source[gen|upload|seed], revision_id, created_at)     -- 不存二进制

revision(id, target_kind[clip|episode|project|skill], target_id,
         prompt, negative, params_json, provider, model_requested, model_used,
         skill_versions_json,          -- 本次注入的 skill 版本，缺它就无法归因
         input_tokens, output_tokens, video_seconds, unit_price, cost, latency_ms,
         motion_json,                  -- {samples, freeze, avg, max}
         acceptance_result_json, parent_revision_id, output_asset_id, created_at)
         -- append-only，绝不 UPDATE

job(id, kind, target_kind, target_id, state, attempts, max_attempts,
    budget_limit, cost_used, idem_key CHAR(64) UNIQUE, error_code, error_detail,
    created_at, started_at, finished_at)
    -- queued → running → waiting_confirm → done | failed | cancelled | paused_budget

lesson(id, scope, failure_type, symptom, fix, evidence_revision_id, hit_count, last_hit_at)
    -- 从失败中学到的**经验**，按项目/类型/模型作用域

skill(id, name, stage[S1..S5], rule_prefix, status, created_at)          -- 全局知识层
skill_version(id, skill_id, version, body_md, sections_json, tool_ref,
              checksum, note, created_by, superseded_at, created_at)
    -- body_md 按节切分，sections_json = [{rule_id:"#110", title, tags[], text}]

presence(project_id, user_id, focus_target, last_seen_at)
lock_row(target_kind, target_id, user_id, expires_at)                    -- 10s TTL
```

**为什么 `clip` 比 v0.2 胖了**：真实产物里一个片段就带「4 个参考图槽位 + 4 条镜头时序 + 2 句原文台词 + 10 条注意 + 逐条验收点」，只留一个 `prompt` 字段会把这些全压成不可校验的字符串。

### 5.1 权限矩阵（7 条）

| 权限 \ 角色 | user | lead | admin |
|---|---|---|---|
| 读自己参与的项目 | ✅ | ✅ | ✅ |
| 读他人项目（只读） | ✅ | ✅ | ✅ |
| 建项目 / 邀请成员 | ❌ | ✅ | ❌ |
| 写项目内容（跑生成、改提示词） | ✅ | ✅ | ❌ |
| 看成本看板（全项目） | ❌ | ✅ | ✅ |
| 配 provider / key | ❌ | ❌ | ✅ |
| **维护 Skill（增改版/发布/停用）** | ❌ | ❌ | ✅ |
| 读 Skill | ✅ | ✅ | ✅ |

> 非对称是故意的：**admin 能改影响所有人的"规则"，但不能改任何人的"内容"**。面试时这比"超管全能"更好讲。

---

## 6. Agent 设计

### 6.1 上下文组装与预算

单次上限 **12k input tokens**，按序填满即停；超限按序裁剪：

| 序 | 内容 | 预算 | 裁剪优先级 |
|---|---|---|---|
| 1 | 系统提示词 + 输出 schema | 1.5k | 永不裁 |
| 2 | 项目卡 + 本集大纲 + 前序 clip 摘要 | 3k | 永不裁 |
| 3 | **Skill 命中节**（见 §7.4） | 2.5k | 只留规则标题 + 前 3 条正文 |
| 4 | 人物卡（姓名/外形锚点/服装） | 3k | 先裁"详述"，留 40 字锚点 + 参考图指派 |
| 5 | 上一集剧情摘要 | 1.5k | 再裁到 300 字 |
| 6 | 最近 3 条命中的 `lesson` | 2k | 3 → 1 条 |
| 7 | 当前 clip 既有提示词 + 参考图槽位 | 1k | 永不裁 |

> 裁的是**长文本描述**，不是**结构性约束**。人物锚点、参考图槽位、镜头时序一律不裁 —— 一裁一致性立刻崩。真实终剪提示词一篇 168 KB（≈7 片段），**任何形式的全文注入都不可能**，所以 Skill 必须按节检索。

### 6.2 Agent 循环（默认/全能共用）

```python
async def run_clip(clip, mode):
    for attempt in range(1, MAX_ATTEMPTS + 1):              # 3
        if ctx.cost_used >= ctx.budget_limit:               # 预算熔断前置
            return Stop("budget_exceeded", attempts=attempt)
        prompt = assemble(clip, skills=recall_skills(clip), lessons=recall(clip, k=3))
        if mode == "default":
            if not await gate_paid_confirm(preview=prompt):  # 付费点硬停
                return Paused(clip)                          # 零计费
        job = await jobs.submit("video", prompt=prompt, res=DRAFT,
                                idem_key=f"{clip.id}:{attempt}:{hash(prompt)}")
        out = await wait(job)
        report = await tools.video_motion_review(out.asset)  # 扫描 + 视觉模型 → 逐条验收点
        failed = [a for a in clip.acceptance if report[a.id] == "fail"]
        if not failed:
            return Ok(attempt, revision=out.revision_id)
        lesson = attribute(report, failed)                    # 结构化归因
        if lesson is None or not lesson.actionable:
            return Escalate(report)                           # 归因不出 → 不瞎重试
        save_lesson(lesson)
        clip.prompt = rewrite(clip.prompt, lesson)             # 定向改写
    return Stop("max_attempts")
```

三条硬规矩：预算检查在循环最前；归因不出来就不重试；全能模式不是"去掉确认"，而是"把确认换成硬上限"。

### 6.3 提示词生成：结构靠规则，内容靠模型

从真实终剪提示词里提炼出的**硬校验器**（违反即不合格，不消耗重试次数，先用规则修）：

| 规则 | 来源 |
|---|---|
| 各镜头时间点加总 == 片段时长；单片段 ≤ 15 s | 剧本 §1、手册 §4.0 |
| 台词**原文内嵌进镜头正文**，不能只写在"台词"段 | `经验教训.md` #94（真实事故：正文没词，模型自己编了"目标体征稳定……"） |
| 参考图**逐项点名**且不得出现"同上/如上/保持一致/连续继承" | 手册 §6.2 |
| 参考图槽位号与用途一致，且**只引用本项目资产**，不许编造 | 真实 `ref_image_0..3` |
| 负面词统一带「无字幕」「避免生成任何文字」 | 终剪提示词头 § |
| 每个片段挂**显式验收点**，否则质检无判据 | 真实"验收点 ✅❌"段 |

模型只负责**画面描述与台词改写**，且必须做字数/秒数配平（4–6 字/秒）。

### 6.4 失败处理矩阵（每格要有测试）

| 失败类型 | 检测 | 策略 | 上限 |
|---|---|---|---|
| 输出非 JSON / schema 不符 | pydantic | 修 JSON 重试 1 次 → 降级 prompt 1 次 | 2 |
| 结构残缺（时序不闭合等） | 规则校验 | **规则修补，不计重试** | — |
| 429 / 5xx | 状态码 | 指数退避 1/2/4s + 抖动 | 3 |
| 超时 | 客户端 timeout | 重试 1 次 → 降级备用 model | 2 |
| 内容审核拒绝 | provider code | 不重试，改提示词后重提 | 1 |
| 预算穿线 | 事前检查 | 立即停，转人工 | — |
| 额度/鉴权失败 | provider code | 不重试，告警到 UI | — |
| 动线 flag（卡顿/静止） | vscan 曲线 | **不自动拒绝**，提示 + 建议 | — |
| 验收点失败 | 视觉模型 | 归因 → 定向改写 → 重跑 | 3 |
| 重复提交 | `idem_key` | 返回既有 job，不重复计费 | — |

### 6.5 记忆（lesson）与 Skill 的分工

| | lesson | skill |
|---|---|---|
| 是什么 | 从**失败**中学到的经验 | 从**成功**沉淀的规则 |
| 谁写 | Agent 自动 | **超管**手动 |
| 作用域 | 项目 / 类型 / 模型 | 全局（按 stage 绑定） |
| 召回 | 同 scope，`hit_count DESC` | 按 stage + clip 特征标签 |
| 膨胀治理 | 同键去重 `hit_count += 1`；单 scope >200 条时归档 `hit_count=1 且 >90 天` | 版本化，旧版 `superseded_at` 保留可回滚 |

> 这条分化就是一面原题"Skill 模块怎么设计"的答案：**知识层与经验层分开，前者版本化受控，后者自动积累可评估**。

---

## 7. Skill 层（超管维护，新）

### 7.1 是什么

一份 Skill = **绑定 stage 的提示词规则包**：`body_md` 按节切分，每节带 `rule_id`（对齐 `经验教训.md` 的编号，如 `#110` 巨物压迫感四件套、`#112` 机位位移、`#114` 形态参考图两用），带 `tags`（`巨物`/`化形`/`开场`/`台词`/`打斗`…）供召回。

现有可迁入的 Skill（不是新写，是把已有文档搬进来）：

| Skill | 来源 | 绑定 stage |
|---|---|---|
| `seedance-final-prompt` | 《Seedance 2.0 系列提示词指南》§2 时序 / §4 运镜 + `经验教训.md` #110–117 | S4 |
| `video-motion-review` | `docs/video-motion-review/SKILL.md`（含 `vscan.ps1`/`vcompare.ps1`） | 质检 |
| `krea2-character-card` | `人物卡\Krea2人物卡提示词.md` | S5 |
| `concept-sheet` | `人物卡\概念设定案提示词.md`（三视图 / 面部特写） | S5 |
| `story-to-script` | 真实 `剧本.md` §1 结构（画幅/画风/时长/铁律/角色消歧） | S3 |

### 7.2 生命周期

```
draft ──dry-run(对 1 个 clip)──▶ active(唯一) ──新版本发布──▶ superseded
```

- 编辑不就地改：**存为新版本**，旧版保留 `superseded_at`，可回滚。
- **发布前强制 dry-run**：拿第一集某个真实 clip 跑一次组装，展示"这次会注入哪几节、占多少 token、prompt 变成什么样"，**但不发起视频生成**（零成本）。这是本功能最关键的交互 —— 改规则的人必须能看见影响面。
- 发布即全局生效，不做灰度（Demo 无此需求）。

### 7.3 与版本链复用

`revision.target_kind` 增加 `skill`，diff/回滚 UI 直接复用 P0-8 的组件 —— **同一套版本机制服务两种对象**，这是省下 3 人日的关键。

### 7.4 注入与召回

1. 按 `stage` 过滤；
2. 用 clip 的 `tags` + 待处理描述与 `sections_json.tags` 匹配打分；
3. 取前 N 节，超预算按 §6.1 裁剪（**只留标题 + 前 3 条正文**）；
4. `revision.skill_versions_json` 记录本次用了哪些 skill 的哪个版本 —— 否则"提示词变好了"无法归因到"是 Skill 改的还是模型换的"。

### 7.5 安全边界（必须写进 README）

- Skill **只允许声明式内容**：markdown 规则 + JSON 参数 + **引用已注册的 tool 名**（allowlist）。
- **禁止上传/内联可执行脚本**。`tool_ref` 只能是代码里注册过的名字，运行时代码随仓库发布、走 code review。
- 理由可正面讲：给了最高权限一个"能执行任意代码"的入口，等于把整个权限体系作废；且工具脚本的变更必须能过代码评审，不能在线改。

---

## 8. 成本模型

视频用量与**时长 × 分辨率**挂钩、与提示词长度无关（实测六次确认）。以此建立可预测的预算：

| 环节 | 通道 | 单价 / 用量 | 单次成本 |
|---|---|---|---|
| 视频草稿 | Seedance mini 720P | **21,675 tokens/秒**；12 s ≈ 260,100 tokens ≈ **0.38 组 ≈ 2.27 元**（≈0.19 元/秒） | 5 s ≈ 1.0 元 |
| 视频正片 | Seedance 720P+ | 待实测（按 `#98` 火山价目） | 待填 |
| 文本（5 环节） | `text.plan` 路由 | 待实测 | 待填 |
| 三视图/面部特写 | `gemini-3.1-flash-image-preview` | 待实测（`liaobots` 账单） | 待填 |
| 精修 | `gpt-image-2.5-sunburst` | 待实测 | 待填 |

**演示预算**：完整演示 ≈ 8 clip × 5 s ≈ 8 元 + 一次全能重试 ≈ 2 元 ≈ **10 元/次**；真实调用录 6 次 ≈ **60 元**；Mock **0 元**。

---

## 9. 评测（golden 来自真实第一集）

- **固定集**：第一集 7 个 clip 的真实分镜 + 5 种类型各 3 条改写（言情/热血/悬疑/校园/职场），共 20 条。
- **指标**（每次改提示词或 Skill 都要跑）：

| 指标 | 定义 | 目标 |
|---|---|---|
| schema 合法率 | 一次通过校验 | ≥95% |
| 时序闭合率 | 镜头时间加总 == 时长 | 100% |
| 台词内嵌率 | 台词原文出现在镜头正文 | 100% |
| 参考图逐项点名率 | 无"同上/保持一致"省略指代 | 100% |
| 台词配平率 | 4–6 字/秒 | ≥90% |
| 首次 attempt 通过率 | 验收点全 pass 且无需重写 | ≥60% |
| 单集成本 / 耗时 | 台账取数 | 记基线不设目标 |
| 人工一致性分 | 抽 8 条按角色锚点漂移 0–2 分 | ≥1.5 |

- 实现：`pytest -m eval`，Mock 下跑（零成本），LLM-as-judge + 人工抽检 8 条校准。
- **Skill 回归**：`skill_version` 变更后自动跑同一集，输出指标对比表 —— 这是"怎么量化证明效果提升了"的直接答案。

---

## 10. 里程碑（6 周 / 2 人：A=后端+Agent，B=前端+UI+集成）

| 周 | A | B | 周末 DoD |
|---|---|---|---|
| W1 | 骨架、13 张表 + Alembic、compose(含 mysql)、认证、权限矩阵、MockProvider、`seed_project.py` | 流水线空壳、三栏布局、Query 层 | 零 key `compose up` 起得来（空 MySQL 自动建表），**真实第一集灌库成功** |
| W2 | `LLMStep`（1 路径 5 模板）、校验器（§6.3 六条）、`revision` 链 | 步骤表单、分镜表编辑器、版本 diff/回滚 UI | 想法 → 分镜表全链路；六条校验全绿 |
| W3 | 图像通道 + 资产库 + 参考图槽位指派 | 资产库、槽位指派交互、镜头墙 v1 | 角色成图，槽位指派进 clip |
| W4 | job 队列/状态机/幂等、预算熔断、成本台账、SSE | 进度条、付费点确认弹窗、成本看板 | 出第一条真实草稿 clip，账对到分 |
| W5 | `video-motion-review` → 逐条验收点、归因、全能模式；**Skill 存储 + 召回 + dry-run** | 质检结果卡（时间码可点）、归因展示、**admin Skill 编辑器** | 演示"错了 → 诊断 → 定向改写 → 重发"；演示"改 Skill → dry-run 看影响面" |
| W6 | presence/乐观锁、trace 收敛、评测集 + 回归 | 协作指示器、README/架构图/3 分钟录屏 | 逐条过 §12 验收清单 |

纪律：每周五只判"**DoD 是否可演示**"。若 W3 结束图像链路未通，立即砍 P1-2 全能模式保 P0。

---

## 11. 3–3.5 分钟演示脚本

> **本文档的脚本是"立项时的设想"。实际演示以 `README.md` 的「3 分钟演示脚本」为准** ——
> 实现之后能力有变化（出图、多集、SSE、trace、打回重发、Skill dry-run 都上了），
> 两份脚本不一致时只维护 README，避免双份脚本各自漂移。

| 时间 | 做什么 | 说什么 |
|---|---|---|
| 0:00–0:20 | 打开已灌入的《师尊别砍我系统》第一集 | 定位：**受控的 Agent 生产链路**；数据全是真实成片，不是演示假数据 |
| 0:20–0:50 | 跑 5 个 LLMStep → 分镜表 | 5 环节一条代码路径；讲上下文 12k 预算怎么裁、Skill 按节注入 |
| 0:50–1:20 | 人物卡（三视图）→ 参考图槽位指派 | 一致性靠"锚点 + 槽位指派"，不靠模型记忆 |
| 1:20–1:50 | 生草稿 → **付费点确认弹窗** | 成本台账：这条 ~1 元；21,675 tokens/秒是可预测的 |
| 1:50–2:30 | 质检 flag → 归因 → 定向改写 → 重发 | ≤3 attempts、预算熔断、归因不出就不重试 |
| 2:30–2:50 | 版本 diff + 回滚 + 成本看板 | 可复现、可审计、可量化 |
| 2:50–3:10 | **admin 改一条 Skill → dry-run → 跑评测看指标变化** | 知识层受控、经验层自动积累；admin 改规则不改内容 |
| 3:10–3:20 | README「设计取舍」 | 为什么不上 Yjs / 不用 Redis / 不用 SQLite / Skill 不执行脚本 |

---

## 12. 验收清单

- [ ] 删掉 `.env` → `docker compose up` 完整演示可跑（Mock + 自带 MySQL）
- [ ] 空 MySQL 自动建表；Alembic 全量迁移在空库可重复执行
- [ ] 中文 / emoji 往返无损（utf8mb4）
- [ ] 2 个 worker 抢同一批 job：无重复执行、无重复计费（`SKIP LOCKED`）
- [ ] 权限 7 条越权测试全绿；**admin 无法写项目内容，但可写 Skill**
- [ ] `seed_project.py` 能把真实第一集灌成 project/episode/clip/shots/acceptance，解析失败带行号
- [ ] 一次生成可追到金额；聚合正确到分
- [ ] 拒绝付费点后零计费、job 为 `cancelled`
- [ ] §6.4 每类失败有注入式测试 + UI 文案
- [ ] 同 `idem_key` 重复提交不双扣费
- [ ] 提示词回滚后重跑，请求参数与目标版一致
- [ ] Skill：新版必须 dry-run 通过才能发布；发布后 `revision.skill_versions_json` 正确记录；可回滚
- [ ] Skill 上传脚本被拒绝（allowlist 校验有用例）
- [ ] `pytest -m eval` 在 Mock 下 ≤2 分钟跑完并输出指标表
- [ ] README 含：架构图、3 分钟录屏、实测成本表、**6 条设计取舍**、零 key 启动（含 lite 素材包获取）、评测结果、roadmap
- [ ] 开源卫生：无 key/无个人路径、`.env.example`、LICENSE、**仓库零二进制**、版权声明
- [ ] `git ls-files` 里没有任何 >1 MB 的文件；`docs/我师父是个普通人/` 未被跟踪
- [ ] `fetch_demo_assets.py --mock-lite`：从零克隆到跑通演示 ≤3 条命令，sha256 校验生效，断点续传可用

---

## 13. 风险登记

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 被追问"协作层为什么这么弱" | 中 | 中 | ADR-002 文本 + roadmap；正面答取舍强于半成品 CRDT |
| 真实 API 涨价/限流（`#98` 已涨 2.5×） | 高 | 中 | `providers` 单层 + 价格表外置；Mock 保障演示不断 |
| Seedance 并发上限、单片段 ≤15 s | 中 | 高 | 分镜阶段按 ≤15 s 切；队列串行（也省钱） |
| 角色漂移（一致性差） | 高 | 中 | 锚点 + 参考图槽位 + 人工一致性分；不承诺"自动一致" |
| **566 MB 素材被误提交进 Git 历史** | 高 | 高 | `.gitignore` 锁 `docs/我师父是个普通人/` 与 `demo/assets/`；仓库零二进制；提交前查 `git ls-files` 有无大文件 |
| Release 分发在国内不可达 / 包被截断 | 中 | 中 | 分卷 + `assets.lock.json` sha256 校验 + 网盘镜像；lite 包 25 MB 保证最坏情况也能演示 |
| **Skill 全文撑爆上下文**（单篇 168 KB） | 高 | 高 | 按节切分 + 标签召回 + 预算裁剪（§6.1 / §7.4）；dry-run 强制显示 token 占用 |
| **Skill 权限与"admin 只读内容"冲突** | 中 | 中 | ADR-008 明确边界：规则可写、内容不可写；权限矩阵单列一行 |
| Skill 变成代码执行面 | 中 | 高 | 只允许声明式 + tool allowlist；禁止脚本上传（§7.5） |
| MySQL 容器拖慢首启 / 对方没 Docker | 中 | 中 | healthcheck + 预热卷；另给 `scripts/dev.sh` 直连本机 MySQL |
| MySQL DDL 非事务性 | 中 | 中 | 只允许加表/加空列/加索引；迁移前 `mysqldump`；CI 空库跑全量 |
| SSE 长连接耗尽连接池 | 低 | 中 | SSE 不持 DB 连接（事件走内存队列）；`pool_size` 显式 + `pool_pre_ping` |
| 6 周做不完 | 中 | 高 | 每周五 DoD 判据 + 砍单顺序：P1-4 → P1-3 → P1-2（Skill 已在 P1-1，不动） |
| Agent 失控烧钱 | 低 | 高 | 预算熔断前置 + `budget_limit` 落库 + 付费点硬停 |

---

## 14. 变更记录

| 版本 | 变化 |
|---|---|
| v0 | 功能愿望清单：无限画布 + 5 类文本节点 + Yjs + 资产/节点版本 + 完整三角色 + 多 provider 网关 + 双模式 Agent |
| v0.1 | 逐条评估，标出 10 项过度设计；4 条判定标准 + 工作量账（14–22 人周 vs 预算 6–8 周）；提出 4 个待确认问题 |
| v0.2 | 冻结：ADR-001~005；10 项 P0 + 4 项 P1；Agent 上下文预算、失败矩阵、记忆治理、成本模型、评测、6 周分工、演示脚本、验收清单、风险登记 |
| v0.2.1 | 数据库定为 MySQL 8.4（ADR-006）：`SKIP LOCKED` 领任务、utf8mb4/`DATETIME(3)`/`CHAR(64)` 约定、compose 纳入 mysql |
| **v0.3** | ① **ADR-007 唯一演示数据源 = 真实第一集**（《师尊别砍我系统》，134 文件/566 MB）：新增 §3 真实剧本基线，`clip` 增 `ref_slots / shots / dialogue / acceptance / parent_clip_id`，原 5 个模板改由真实产物反推，Mock fixture 与评测 golden 全部取材于此，仓库只收 330 KB 文本；② **ADR-008 超管维护 Skill**：新增 §7（生命周期/dry-run/召回/安全边界）、`skill` + `skill_version` 两表、权限矩阵 6→7 条且 admin 对内容仍只读、上下文预算新增 Skill 节、`revision.skill_versions_json` 保证可归因、评测加 Skill 回归；③ 为 Skill 让路砍 3 项（§2.4）；④ 成本模型补 21,675 tokens/秒 的实测常数 |

| **v0.3.1** | 素材分发定案：**仓库零二进制**，`fetch_demo_assets.py` 三档（`--mock-lite` ~25 MB 默认 / `--full` ~566 MB / `--local`），GitHub Release 分卷 3×~190 MB + 网盘镜像，`assets.lock.json` 固定 sha256；连带改 §3.3、风险登记、验收清单（`git ls-files` 无 >1 MB 文件） |

**下一版（v0.4）**：把 `Skill` 的 `sections_json` 格式、`providers` 适配层接口、SSE 事件格式写成正式契约，并填入实测成本数字。
