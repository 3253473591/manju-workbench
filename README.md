# 漫剧工作台 · 一个受控的 AI 短剧生产链路

把一句原始想法推到成片：**分集剧情 → 剧本分镜 → 终剪提示词 → 出图 → 逐片段视频 → 动线 + 视觉质检
→ 失败归因 → 打回重发**。

Agent 负责编排与自愈，人只在**付费点**和**审批**上做决定。

不是通用画布工具，也不是商用漫剧平台。它的目标是**每条链路都能被追问**：每次生成可追到金额，
每次失败有归因，每次重发有停止条件，每次改规则有 dry-run 与 golden 回归兜底。

> 求职作品集项目，按"非企业级 Demo"设计：单机、2–5 人协作、**零 key 可跑**、三条 provider 通道可分别接真。

---

## 目录

| 路径 | 内容 |
|---|---|
| `apps/api` | FastAPI + SQLAlchemy + Alembic（14 张表） |
| `apps/web` | React + Vite + TanStack Query（复用 `prototype/` 的视觉系统） |
| `scripts/seed_project.py` | 解析真实剧集目录 → 灌库（幂等，支持多集） |
| `scripts/seed_skills.py` | 把规则文档灌成 Skill（5 份 / 21 节） |
| `demo/outline_铜柱之下.md` | 手写原始大纲（流水线的真实输入示例） |
| `docs/开发文档/` | 约定、数据结构、API 协议、异常与边界、对抗性探针（**改字段只改这里**） |
| `docs/PRD v0.3.md` | 范围与取舍（含被砍掉的东西和原因） |
| `prototype/` | 静态视觉原型（设计源） |

---

## 现在的功能（一眼扫）

**内容链路**

- 五环节一条代码路径：S1 总大纲 / S2 分集剧情 / S3 剧本分镜 / S4 终剪提示词 / S5 人物卡
- **出图**：S5 人物卡 → 真出图 → 进项目资产库 → **参考图槽位指派**（跨集复用同一张图）
- **多集**：一个项目多集，片段序号按集独立；成本可按集聚合；镜头墙可看全项目
- **资产库**：项目级素材列表（图片/视频），删除带引用检查 —— 删不掉时直接列出「哪个集哪个片段的哪个槽位在用」
- **出片**：付费点确认 → 入仓（补宽高/时长/海报）→ 动线质检（vscan）→ 内容级判据交视觉模型
- **打回 / 重发**：`review → rejected → reroll`，重发链路记 `parent_revision_id`
- **版本链**：take 列表、两版 diff、回滚（回滚不写新 revision、不花钱）

**工程面**

- 状态机 + 幂等键 + `If-Match` + 项目级串行 + 预算前置；失败落终态，不空转
- 成本台账（模型 / 集 / 片段 / 天四个口径），金额可追到分
- **Skill 层**：admin 维护，新建版本 → dry-run（按注入关系选 stage）→ publish → rollback
- **SSE** 实时推送（`Last-Event-ID` 补发 + `resync`）；**跨副本**靠 `event_log` 轮询兜底（延迟 ≤ 0.5 s），单副本仍是直推
- **job 时间线**（trace）：`claim → provider → asset → review → decision`，每步带耗时
- **三条 provider 通道**各自独立：文本/视觉（OpenAI 兼容）、视频（Ark Seedance）、出图（OpenAI Images）
- **止血开关**：admin 可在「Skill 维护」页停用任一条通道（落 `runtime_flag` 表，多副本共享），停用后调用点在花钱前 503

---

## 快速开始（Docker，零 key）

```bash
# 1. 起 mysql + api + web（凭据都有内置默认值，不需要 .env）
docker compose up --build

# 2. 打开 http://localhost:8080 ，用下面的演示账号登录
```

不给真实 API key 也能跑全程：文本/视觉/出图/视频全部走 **mock** —— 视频按实测常数折算用量
（21,675 tokens/秒、0.189167 元/秒）并回放你目录里的真实成片，出图复制一张已有素材，
**链路、成本台账、质检、版本链都是真的，只是不花钱。**

### 把自己的剧本灌进去

素材**不随仓库分发**。把剧集目录挂进去（默认 `./media`），结构参照：

```
media/
  <你的剧集>/
    人物设定.md
    第一集剧情.md
    第一集/剧本.md
    第一集/Seedance2.0终剪提示词.md
    00_静态资源/*.png
```

**灌库在宿主机跑**（脚本要读宿主上的剧集目录和素材），DSN 指向 compose 暴露的 mysql：

```powershell
docker compose up -d
$env:DATABASE_URL='mysql+pymysql://root:devpass@127.0.0.1:3306/manju_dev?charset=utf8mb4'
python scripts/seed_project.py --source <你的剧集目录> --load --thumbs
python scripts/seed_skills.py        # Skill 不灌，steps/*/run 就没有规则可注入
```

> 3306 被本机 MySQL 占用时，起服务加 `MYSQL_PORT=3311`，DSN 端口跟着改。
> 空库时前端引导你建项目，不是白屏。

`--load` 是幂等的：重跑不会多出片段，也不会往只增不改的 `revision` 里追加第二份。
`--thumbs` 用 ffmpeg 给**视频和图片**都做海报（写 `.thumbs/`，视频平均 47 KB/张；图片能把
9.6 MB 的原图压到 69 KB）。

**多集**：`--episode N` 把第 N 集灌进**同一个项目**（`第一集`/`第2集` 两种目录命名都认）。
成片与 `00_静态资源/` 下的人物图全部按**项目级资产**入库 —— 同一张图在第二集再次被引用时不复制文件，
只是多个片段的槽位指向同一个 `asset_id`：

```powershell
python scripts/seed_project.py --source <你的剧集目录> --load --thumbs            # 第一集
python scripts/seed_project.py --source <你的剧集目录> --load --episode 2        # 第二集（同项目）
```

### 本地开发

```powershell
# 后端（8000）：MEDIA_ROOT 必须等于 seed 的 --source，否则取图全 404
$env:JWT_SECRET='dev-secret-dev-secret-dev-secret-32'
$env:CSRF_ORIGINS='http://localhost:5173,http://127.0.0.1:5173'
$env:MEDIA_ROOT='<你的剧集目录>'
$env:VSCAN_PATH='D:\AIGC\ComfyUI-aki-v2\tools\vscan.ps1'   # 可选；不设则动线质检降级
$env:VISION='0'                                            # 可选；关掉视觉判定（默认开）
python -m uvicorn manju.main:app --app-dir apps/api --port 8000 --reload

# 前端（5173）：/api 走 Vite 代理，目标可用 VITE_API_TARGET 覆盖
cd apps/web; pnpm install; pnpm dev
```

### 接真 provider（可选，默认全 mock）

三条通道**各自独立**，可以只接其中一条（例如只接视觉，出片仍用 mock 回放）：

```powershell
# ① 文本 + 视觉：任何 OpenAI 兼容服务（实测 DeepSeek）
$env:PROVIDER='openai'
$env:PROVIDER_BASE_URL='https://api.deepseek.com'
$env:PROVIDER_API_KEY='sk-...'
$env:PROVIDER_TEXT_MODEL='deepseek-flash'
$env:PROVIDER_VISION_MODEL='deepseek-flash'   # 视觉判定（联络图）；不填就回落文本模型

# ② 视频：火山方舟 Seedance（真出片）
$env:PROVIDER_VIDEO='ark'
$env:PROVIDER_VIDEO_API_KEY='ark-...'
$env:PROVIDER_VIDEO_MODEL='ep-...'              # 推理点 id
$env:PROVIDER_VIDEO_RESOLUTION='720p'
$env:PROVIDER_VIDEO_PRICE_PER_1K_TOKENS='0.008727'

# ③ 出图：任何 OpenAI Images 兼容服务
$env:PROVIDER_IMAGE='images'
$env:PROVIDER_IMAGE_BASE_URL='https://ai.example.com/v1'
$env:PROVIDER_IMAGE_API_KEY='...'
$env:PROVIDER_IMAGE_MODEL='gpt-image-2'
```

启动日志会逐条打印哪条通道是真调用、哪条还是 mock：

```
provider=openai ｜ text=deepseek-flash(真调用) ｜ vision=deepseek-flash(真调用)
               ｜ video=doubao-seedance-2-0-mini(真调用) ｜ image=gpt-image-2(真调用)
```

配错/缺项**启动即失败**（`03` §12.1：宁可起不来，不要带病跑）；admin 账号在「Skill 维护」页顶部
能点**连通性检查**（真打一次最小请求）。

> **真视频是按 token 计费，不是按秒**：实测 2.0mini / 480p / 4 s = 40,594 tokens ≈ **0.354 元**。
> 所以 `revision.unit_price` 记 `NULL`（不造"元/秒"的伪口径），成本用 `PROVIDER_VIDEO_PRICE_PER_1K_TOKENS` 折算。

### 演示账号

| 账号 | 口令 | 角色 | 能写内容？ |
|---|---|---|---|
| `seed@example.com` | `seedpass12345` | lead（项目 owner） | ✅ |
| `admin@example.com` | `admin12345` | admin | ❌ 只读（只能改 Skill / provider） |

> 口令写在代码里是有意的（Demo 要能直接给账号）；换真实部署前必须改。
> `admin` 改不了内容不是 bug —— 权限矩阵里"最高权限不能改内容"是刻意设计的。

---

## 架构

```
apps/web (React)  ──HTTP / SSE──▶  apps/api（鉴权 · 权限 · 形状校验 · 错误封装）
                                        │
                            service / domain（时序闭合 · 台词内嵌 · 槽位连续 · 成本口径）
                                        │
      ┌───────────────────┬─────────────┼──────────────┬────────────────────┐
 providers(text/vision) providers(video) providers(image)  jobs（状态机·幂等·预算）  skills / lessons
      └───────────────────┴─────────────┴──────────────┴────────────────────┘
                                   MySQL 8.4（16 表，含 job_trace / runtime_flag / event_log）
```

分层规矩：`providers` 不认识 clip/project（只认"一次调用 + 参数 + 预算"）；`steps` 不认识 HTTP；
`apps/api` 不写业务计算。通道之间也守边界 —— 出片用 `PROVIDER_VIDEO`，视觉判定用 `PROVIDER`
（曾经复用出片 provider，导致"配了真视觉还在用 mock 判"，这是接真 key 才暴露的缺陷）。

**Agent 循环**（默认/全能共用一套）：

```
组装上下文（12k 预算；clip 级步骤带镜头表 + 台词逐字清单 + 验收点）
  → 调 provider（每次调用前查预算；输出契约 = pydantic schema 原文）
  → 结构化校验（失败即 422 VALIDATION_INVARIANT，不猜）
  → 落 revision（append-only，含 token/秒数/单价/金额，并记 parent_revision_id）
  → 动线质检（vscan）→ 判不了的判据交视觉模型 → 仍判不了保持 unknown
  → 失败且归因可执行：写 lesson + 定向改写 prompt + 同一 job 重排（≤3）
  → 归因不出 / 次数用尽：转人工（failed + ESCALATED）
```

---

## 实测数字（不是估算）

**真实剧集基线**（《师父是个普通人》第一集，灌库后）：

| 项 | 值 |
|---|---|
| 片段 / 镜头 / 参考槽位 | 8 / 34 / 36 |
| 台词 / 验收点 / 生成尝试 | 21 / 13 / 17 次（v1…v4 重跑都在账上） |
| 累计成本（mock 口径） | 40.00 元（0.189167 元/秒） |
| 视频用量常数 | **21,675 tokens/秒**（六次复现，与提示词长度无关） |
| 单次 12 s 成片 | 260,100 tokens ≈ 2.27 元，约 3 分钟 |

**真 provider 实测**（三条通道都真打过）：

| 通道 | 实测 |
|---|---|
| 文本 S1–S4（deepseek-flash） | 手写 200 字大纲 → 一集 66 s / **7 个片段**（每片 3 镜、4 条验收点）；S4 约 2.4–2.9 s / 255→276 tokens |
| 视觉质检（deepseek-flash） | 6 帧联络图 13.2 s / 639 tokens；能指出"光环只在第 2/5/6 帧"、区分"剧情内 UI"与"烧入字幕" |
| 视频（Ark Seedance 2.0mini） | 4 s / 480p：**90 s 出片**、40,594 tokens ≈ **¥0.354**、864×496 / 含音轨 |
| 出图（gpt-image-2 low 1024²） | **40 s** / 1.58 MB PNG / 缩略图 53 KB；人物卡可直接进槽位 |
| 多集 | 第一集 8 片段 + 第二集 5 片段同项目；成本按集 `第1集 ¥50.78 / 第2集 ¥2.65` |

**测试**：`521 passed / 1 skipped`（真 MySQL，每个用例事务回滚），其中
**20 条 golden 契约回归** + **164 条对抗性探针** + **35 条 provider 适配**。

> 文本/视觉目前 `revision.cost` 记 0、`unit_price` 记 `NULL`：文本单价还没实测，
> **宁可缺价格，不可编价格**；`input_tokens/output_tokens` 是上游真实 usage。

---

## 设计取舍（被砍掉的东西同样重要）

| 决定 | 为什么 | 代价 |
|---|---|---|
| **三条 provider 通道分开配置** | 文本/视觉、视频、出图的上游形态完全不同；绑在一起会出现"换一家就得全换" | 三组环境变量 |
| **模型输出契约写进 prompt（pydantic schema）** | 不给契约，模型会自己包信封（实测返回 `{clip, edit, output…}`） | 每次调用多几百 token |
| **`unknown` 不算通过；零验收点才放行** | 判不了 ≠ 通过；但"没有任何可机检项"不该把出片永远堵住 | 演示里仍有片段停在 review |
| **自动重试只对 `?mode=auto`** | 默认模式的语义是"付费点停下等人" | 默认模式失败即 `failed` |
| **视频按 token 记账，`unit_price=NULL`** | 上游就是按 token 收钱，记"元/秒"等于编口径 | 成本看板要按 tokens 折算 |
| **集删除不重编号** | 集号会出现在链接、文档、人脑里，静默重排会让它们一起错位 | `idx` 留洞（第 1/第 3 集） |
| **出图同步返回，不排 job** | 10~40 s、无付费点确认，排 job 只多一层轮询 | 长请求占一个连接 |
| **`/admin/providers` 只读** | 配置来自环境变量，进程内改不动；假开关比没有更危险 | 换 key 要重启 |
| **不引 Yjs / Redis / Celery** | 2–5 人 + `SKIP LOCKED` 足够 | 不能水平扩容 worker |
| **不引 Tailwind** | 原型是手写 CSS，两套一起上只会打架 | 与 PRD 原始选型有偏差 |
| **Mock 回放真实成片** | 演示不受网络与钱包摆布，且数字真实 | Mock 不做创作，只回放结构 |

完整版在 `docs/开发文档/02-API协议.md` §11.1（判断 / 为什么 / 代价 / 在哪验证）。

---

## 3 分钟演示脚本

> **口径**：录屏用全 mock（`PROVIDER*` 不配），才能保证 3 分钟跑完 —— 真出图 40 s、真出片 ~90 s，
> 现场真跑会把时间吃光。但要说清哪些是真：链路、幂等、预算、质检、版本链、成本、权限、SSE 都是真的，
> 只有"画"和"出片"是回放；真 provider 的数字在「实测数字」里，不靠现场出片证明。
>
> **准备**
> - `seed_project.py --load --thumbs`（第一集）+ `--episode 2`（第二集，演跨集复用）+ `seed_skills.py`
> - `VSCAN_PATH` 指向 `vscan.ps1`；不设也能跑，但动线质检会降级成 `unknown`（现场正好解释"判不了 ≠ 通过"）
> - **打回只在 `review` 态可点**：库里第一集有 2 个 review 片段（6 approved），用它们，别用刚 mock 生成的
>   （没有验收点的片段会被 `no_criteria` 直接放行到 approved）
> - 现有的《铜柱之下》就是"手写大纲 → S1–S4 真模型"跑出来的：1 集 7 片段，可当 0:15 那拍的事后证据
> - **开录前跑自检**：`python scripts/demo_preflight.py --api http://127.0.0.1:8000`（16 项：迁移/集/ review 片段/
>   可回滚 take/跨集资产/ Skill /四通道启用/MEDIA_ROOT）。逐拍的执行卡与兜底见 `docs/演示检查单.md`；
>   想先空跑一遍：`python scripts/demo_rehearsal.py --api http://127.0.0.1:8000`（9 拍，约 9 秒）

| 时间 | 操作 | 说什么 |
|---|---|---|
| 0:00–0:15 | 打开「流水线」，项目选《师父是个普通人》第一集 | 定位：**受控的 Agent 生产链路**；数据是真实成片，不是编的 |
| 0:15–0:40 | 「＋项目」建空项目 →「＋分集」→ 依次「运行 S1」「运行 S2」「运行 S3」 | 五环节**共用一条代码路径**；12k 上下文 + Skill 按节召回；S3 直接写进一批片段（《铜柱之下》就是这条链路产出的 7 段） |
| 0:40–1:00 | 在该项目「运行 S5」→ 点「用人物卡出图」 | 人物卡 → 出图 → 进项目资产库；mock 下会**命中已有素材**（`deduped=true`，面板如实标「mock 回放」），真出图 40 s |
| 1:00–1:20 | 切回《师父》→ 第二集 → 分镜表点「槽位」→ 选第一集用过的参考图 | 资产是**项目级**的：两集共用同一个 `asset_id`，不复制文件（库里 asset 6 就被两集引用） |
| 1:20–1:45 | 第二集选一个有提示词的片段 →「生成…」→ 付费点弹窗看预估 →「确认付费」 | 确认前零计费；出片按 token 记账；状态是 SSE 推的（单副本毫秒级，多副本 ≤0.5 s 兜底） |
| 1:45–2:05 | 出片后看质检结论（pass/fail/unknown 三色）+ 执行时间线 | 动线判"画面在不在动"，内容级判据交视觉模型；**`unknown` 不等于通过**，判不了的片段留在 review |
| 2:05–2:30 | 回第一集，点一个 review 片段的「打回」→「重发…」→ 弹窗里看版本链 →「回滚到此版」 | 审批绑定在具体 take 上；重发记 `parent_revision_id`；回滚**不写新 revision、不花钱**（库里有 21 个可回滚 take） |
| 2:30–2:50 | 切「资产库」→ 删一张在用素材（被 409 拦住，引用方含另一集）→ 切「成本台账」按集 | 资产不级联删，删不掉时列出"哪个集、哪个片段、哪个槽位"；成本可切**按模型/按集/按片段/按天** |
| 2:50–3:10 | 换 admin 登录 →「Skill 维护」→ 改一版 →「试跑」→ 看影响面 →「发布」 | 知识层受控：没试跑发布不了；试跑只组装、不调用上游（页面上方还有 provider 面板：文本/视觉/视频/出图四条通道的启用状态） |

> **备用 A（约 20 s，被追问或时间富余时演）：止血开关**
> admin 在 provider 面板对 `text` 点「停用」（必须填原因）→ 换 owner 回「流水线」点「运行 S1」→
> `503 PROVIDER_DISABLED` 并显示"该通道已被管理员停用：原因"→ 换 admin 点「启用」→ 再点成功。
> 要点：闸在**花钱之前**（出图/出片/文本三条入口都埋了），状态存 `runtime_flag` 表、多副本共享。
>
> **备用 B（不占屏幕，口头说）：SSE 跨副本**
> 两个 uvicorn 共享同一 MySQL：B 副本发布的事件，A 副本的 SSE 流在 ≤0.5 s 内补上
> （`event_log.id` 就是 `Last-Event-ID`）；单副本仍是进程内直推，不受轮询影响。
>
> **备用问答**：`job` 时间线（`claim→provider→asset→review→decision`）／`03` 的失败矩阵／
> `04` 的四轮 16 个缺陷（含并发入仓、删集漏记、日预算未接、S2 覆盖分镜）／`05` 的前端验收记录。

---

## 接口边界

`/api/v1` 下已实现：`auth` / `projects` / `episodes`（含删除、重排）/ `clips`（含 `rollback`、`reroll`）/
`revisions` / `assets`（含删除，被引用即禁删）/ `steps/*/run` / `projects/{pid}/images`（出图）/ `skills`（读 + 写：新建版本 /
dry-run / publish / rollback / 停用）/ `jobs`（含 `trace`）/ `projects/{pid}/events`（SSE）/
`admin/providers`（只读 + 连通性自检）/ `locks` / `presence` / `cost` / `health`。

**没实现的端点没有路由**（404），不留占位实现。完整清单见 `docs/开发文档/02-API协议.md` §11。

## 测试

```bash
cd apps/api && python -m pytest -q          # 需要本地 MySQL（会自动建 manju_test 库并跑迁移）
```

- **20 条 golden 契约回归**（`test_golden_regression.py`）：改 Skill / 模板 / 状态机后跑它，
  只断言契约不断言模型措辞，所以 mock 也能跑、零成本可重复
- **164 条对抗性探针**（`test_adversarial*.py`）：越界、空值、错类型、脏 DB 行、上游故障；
  撞出来的 9 个缺陷与刻意不改的行为见 `docs/开发文档/04-对抗性探针与缺陷记录.md`
- **35 条 provider 适配**：用 `httpx.MockTransport` 造上游，**不需要真 key** 也能钉住解析/计费/错误分类

CI（[.github/workflows/ci.yml](.github/workflows/ci.yml)）跑四件事：后端 pytest（真 MySQL）、前端 `tsc + vite build`、
配置项审计（`scripts/settings_audit.py --strict`，抓"写了没人读/读了没写"的配置）、以及 Playwright 冒烟
（登录 → 分镜表 → 付费点，用 `scripts/seed_e2e.py` 造最小数据，不需要剧集素材）。

```bash
cd apps/web && pnpm exec playwright test --list   # 本地看用例
python scripts/settings_audit.py --strict         # 本地跑配置审计
```

## 已知未做 / 路线图

- `reroll` 的"重拍成新片段"分支（现在是在同一片段上累积 take，`parent_clip_id` 未用）
- 文本/视觉的单价实测（现在 `cost=0`、`unit_price=NULL`）

> 多集、出图、SSE、trace、Skill 维护、reroll 都已落地；《师父是个普通人》一二集（8 + 5 片段）
> 与手写大纲《铜柱之下》跑出的新项目（1 集 7 片段）都在 `manju_dev` 里。

## License

MIT
