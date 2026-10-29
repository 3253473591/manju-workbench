# 02 · 前后端 API 协议

> 权威接口契约。改端点、改错误码只改这里。
> 错误封装的**唯一形状**、金额/时间/分页的传输格式见 `00` §3。本文只规定"有哪些接口、收什么、回什么、什么情况下报什么错"。

---

## 1. 基础

| 项 | 值 |
|---|---|
| Base path | `/api/v1` |
| 编码 | `Content-Type: application/json; charset=utf-8`（上传除外） |
| 认证 | `Cookie: dsh_session=<JWT>`，`HttpOnly; SameSite=Lax; Secure(生产)`；有效期 7 天，无 refresh |
| CSRF | 仅对 `POST/PATCH/DELETE` 校验 `Origin`，白名单来自配置；不引 token 双提交 |
| 请求追踪 | 客户端可带 `X-Request-Id`，不带则服务端生成；响应必回同值 |
| 版本 | 前缀 `/v1` 即版本。单前端同仓部署，不承诺并行多版本；破坏性变更直接改 `/v2` |
| 压缩 | 不主动 gzip（JSON 小，收益低于调试成本） |

**演示账号**（seed 灌出来的，口令写死在代码里是有意的 —— 换真实部署前必须改）：

| 账号 | 口令 | 角色 | 能写项目内容？ |
|---|---|---|---|
| `seed@example.com` | `seedpass12345` | lead，第一集的 owner | ✅ 可写、可跑生成 |
| `admin@example.com` | `admin12345` | admin | ❌ 只读（只能改 Skill / provider） |

本地起服（`JWT_SECRET`、`CSRF_ORIGINS`、`MEDIA_ROOT` 缺失时启动即 `exit 1`，见 `03` §12.1）：

> 一键起栈（mysql + api + web，零 key）见仓库根 `README.md`；下面是单进程调试用的起法。

```powershell
$env:JWT_SECRET='dev-secret-dev-secret-dev-secret-32'
$env:CSRF_ORIGINS='http://localhost:5173'
$env:MEDIA_ROOT='docs\我师父是个普通人'   # 必须等于 seed 的 --source，否则 /assets/{id}/file 全 404
$env:VSCAN_PATH='D:\AIGC\ComfyUI-aki-v2\tools\vscan.ps1'  # 可选；不设则质检降级为 unknown
uvicorn manju.main:app --app-dir apps/api --port 8000 --reload
```

### 1.1 通用响应头

```
X-Request-Id: req_01J9Z8K2QW3E
ETag: "7"                        # 仅单资源 GET，值 = 该资源的 version
Idempotency-Replayed: true       # 仅重放命中时出现
X-Budget-Remaining: 8.720000     # 仅生成类响应，便于前端预判
```

---

## 2. 认证与权限

### 2.1 权限判定顺序（唯一算法，不得各端点自行发挥）

```
1. 未登录              → 401 AUTH_REQUIRED
2. 账号 is_active=0    → 403 AUTH_DISABLED
3. 资源不存在          → 404 NOT_FOUND（不区分"无权看"与"不存在"，防探测）
4. 判定 read:
     项目所有者      → ✅
     非 owner 的登录用户 → ✅（v0 一律只读他人项目，PRD §4.2）
5. 判定 write（改内容 / 跑生成）:
     owner 或 project_member.role='member' → ✅
     project_member.role='viewer'          → 403 PERM_READONLY_PROJECT
     其它                                  → 403 PERM_READONLY_PROJECT
6. 判定项目级管理（建项目 / 邀请成员）:
     user.role ∈ {lead, admin} 且（admin 仅在 skill/provider 上另有权限）→ 见 §2.2
7. 判定全局管理（provider / skill）:
     user.role='admin' → ✅ 否则 403 PERM_ADMIN_ONLY
```

**admin 的特殊性**：`admin` 对**所有项目只读**（不能跑生成、不能改提示词），只对 **Skill** 与 **provider 配置** 有写权限。这是 PRD ADR-008 的落地，代码里表现为 `can_write_project()` 与 `can_write_global()` 两个**不同**函数，不得合并成一个 `is_admin` 判断。

### 2.2 角色 × 动作矩阵（与 PRD §5.1 一一对应；PRD 里"读自己项目/读他人项目"两行在此合为一行"读任意项目"）

| 动作 | user | lead | admin |
|---|---|---|---|
| 读任意项目 | ✅ | ✅ | ✅ |
| 写自己参与的项目 | ✅ | ✅ | ❌ |
| 建项目 | ❌ | ✅ | ❌ |
| 邀请/移除成员 | ❌ | ✅（仅自建项目） | ❌ |
| 看全项目成本看板 | ❌ | ✅ | ✅ |
| 配 provider | ❌ | ❌ | ✅ |
| 维护 Skill | ❌ | ❌ | ✅ |

---

## 3. 端点总表

`I` = 需 `Idempotency-Key`；`M` = 需 `If-Match`；`S` = 可能触发外部调用（花钱）。\n\n> 偏差：`steps/*/run` 这五条**目前不要求幂等键**。文本 step 是同步执行、成本为 0、写回是可覆盖字段，加幂等键收益为零；等文本单价落地、需要防重复扣费时再补（见 §11 取舍 5）。

### 3.1 认证

| 方法 | 路径 | 权限 | |
|---|---|---|---|
| POST | `/auth/register` | 匿名 | |
| POST | `/auth/login` | 匿名 | |
| POST | `/auth/logout` | 登录 | |
| GET | `/auth/me` | 登录 | |

### 3.2 项目

| 方法 | 路径 | 权限 | |
|---|---|---|---|
| GET | `/projects` | 登录 | |
| POST | `/projects` | lead+ | |
| GET | `/projects/{pid}` | 登录 | |
| PATCH | `/projects/{pid}` | owner | M |
| POST | `/projects/{pid}/archive` | owner | |
| GET | `/projects/{pid}/members` | 登录 | |
| POST | `/projects/{pid}/members` | lead+(自建) | |
| DELETE | `/projects/{pid}/members/{uid}` | lead+(自建) | |

### 3.3 集与片段

| 方法 | 路径 | 权限 | |
|---|---|---|---|
| GET | `/projects/{pid}/episodes` | 登录（不分页） | |
| POST | `/projects/{pid}/episodes` | write | |
| GET | `/episodes/{eid}` | 登录 | |
| PATCH | `/episodes/{eid}` | write | M |
| DELETE | `/episodes/{eid}` | write | 删集：片段随外键级联；**不重编号**；有付费产物 → `409 CONFLICT_HAS_REVISIONS` |
| POST | `/episodes/{eid}/move` | write | `{to_idx}`；插入式平移 + 空槽占位（绕开 `uk_ep_idx`） |
| GET | `/episodes/{eid}/clips` | 登录（不分页） | |
| GET | `/projects/{pid}/clips` | 登录（游标分页） | 项目级镜头墙：跨集拉全部片段，`episode_id` 够前端回查集号 |
| POST | `/episodes/{eid}/clips` | write | |
| GET | `/clips/{cid}` | 登录 | |
| PATCH | `/clips/{cid}` | write | M |
| DELETE | `/clips/{cid}` | write | 有付费产物 → `409 CONFLICT_HAS_REVISIONS`（`revision` 没有 FK，删了会漏记） |

### 3.4 流水线步骤（5 个 stage 各自一条路由，便于逐个鉴权）

| 方法 | 路径 | 权限 | |
|---|---|---|---|
| GET | `/steps/templates` | 登录 | |
| POST | `/projects/{pid}/steps/S1/run` | write | I S |
| POST | `/projects/{pid}/steps/S5/run` | write | I S |
| POST | `/episodes/{eid}/steps/S2/run` | write | I S |
| POST | `/episodes/{eid}/steps/S3/run` | write | I S |
| POST | `/clips/{cid}/steps/S4/run` | write | I S |

### 3.5 生成与 job

| 方法 | 路径 | 权限 | |
|---|---|---|---|
| POST | `/clips/{cid}/generate` | write | I M S |
| POST | `/clips/{cid}/reroll` | write | I M S |
| GET | `/jobs/{jid}` | 登录 | |
| GET | `/jobs/{jid}/trace` | 登录（只读） | |
| GET | `/jobs?target_kind=&target_id=` | 登录 | |
| POST | `/projects/{pid}/images` | write（owner/member） | 出图（人物卡/参考图）：同步返回，产物直接进项目资产库 |
| POST | `/jobs/{jid}/confirm` | write | |
| POST | `/jobs/{jid}/cancel` | write | |
| POST | `/jobs/{jid}/retry` | write | |

### 3.6 版本与成本
| 方法 | 路径 | 权限 | |
|---|---|---|---|
| GET | `/clips/{cid}/revisions` | 登录 | |
| GET | `/revisions/{rid}` | 登录 | |
| GET | `/revisions/{a}/diff/{b}` | 登录 | |
| POST | `/clips/{cid}/rollback` | write | |
| GET | `/projects/{pid}/cost?group_by=&from=&to=` | lead+ / admin（见 §2.2） | |
| GET | `/projects/{pid}/lessons` | 登录 | |

三个**派生字段**专为界面而加（都不落库，真相仍在原表）：

| 字段 | 出处 | 用途 |
|---|---|---|
| `revision.seq` | 按 `id` 顺序数同一 target 内的第几版 | 前端显示 "v3"（`revision` 表没有 version 列，只增不改） |
| `clip.cost_total` | `sum(revision.cost)` where `target_id = clip.id` | 分镜表/镜头墙的每片段金额 |
| `ref_slots[].url` / `thumb_url` | 槽位 `asset_id` 对应的取图地址 | 槽位 4-up 直接出图，不必再查资产 |

`cost` 的 `group_by` 支持 `model` / `day` / `clip`；`clip` 分组的 key 是 `clip:{id}`，前端拿 id 回查标题。
`day` 的 key 是 **UTC 日期**（`03` §6.4），前端负责转本地时区，别在后端猜。`stage` 仍不支持（阶段还没落到 revision 上）。

### 3.7 Skill（超管）

| 方法 | 路径 | 权限 | |
|---|---|---|---|
| GET | `/skills` | 登录 | |
| GET | `/skills/{sid}` | 登录 | |
| GET | `/skills/{sid}/versions` | 登录 | |
| GET | `/skills/{sid}/versions/{v}` | 登录 | |
| POST | `/skills/{sid}/versions` | **admin** | |
| POST | `/skills/{sid}/versions/{v}/dry-run` | **admin** | |
| POST | `/skills/{sid}/versions/{v}/publish` | **admin** | |
| POST | `/skills/{sid}/rollback` | **admin** | |
| PATCH | `/skills/{sid}` | **admin** | M |

### 3.8 管理 / 协作 / 健康

| 方法 | 路径 | 权限 | |
|---|---|---|---|
| GET | `/admin/providers` | admin | |
| POST | `/admin/providers/check` | admin | 真打一次上游（最小请求）验 key |
| POST | `/admin/providers/{channel}/state` | admin | 运行时开关：`{enabled, reason}`；停用后调用点 503 `PROVIDER_DISABLED` |
| POST | `/locks/acquire` | write | |
| POST | `/locks/release` | write | |
| POST | `/presence/heartbeat` | 登录 | |
| GET | `/projects/{pid}/events` | 登录 | SSE |
| GET | `/health` | 匿名 | |

---

### 3.9 资产

| 方法 | 路径 | 权限 | |
|---|---|---|---|
| GET | `/assets?project_id=&kind=&limit=&cursor=` | 登录（项目可读） | |
| GET | `/assets/{aid}` | 登录（项目可读） | |
| GET | `/assets/{aid}/file?thumb=0\|1` | 登录（项目可读） | |
| DELETE | `/assets/{aid}?purge=0\|1` | write | 被引用 → `409 CONFLICT_ASSET_REFERENCED`；`purge=1` 只清 `.gen/` 下的派生文件，种子/上传素材不动 |

资产删除**不做级联**：被片段槽位（`detail.usages[].clip_id/slot_idx`）或某次 take 的产物
（`detail.usages[].revision_id`）引用时直接 409 —— 悬空槽位比"删不掉"更难查。
物理文件默认保留：`MEDIA_ROOT` 就是用户的剧集目录，种子/上传素材删了没有第二份。

`project_id` 必填：跨项目混列既没有 UI 也没有权限语义。`kind` 可选（`image` / `video` / `audio`），游标按 `id DESC`。

`clip` 出参里多了个**派生字段** `output_asset_id`（= 当前 revision 的产物资产），镜头墙要的海报就靠它：

```
GET /episodes/{eid}/clips → [{ ..., "title": "荒诞接生与功德圆满", "current_revision_id": 2, "output_asset_id": 62 }]
GET /assets/62 → { url, thumb_url, kind: "video", ... }
GET /assets/62/file?thumb=1 → 缩略图（没有则回落原片并标 X-Thumb-Fallback: 1）
```

不建 `clip.output_asset_id` 列：真相在 `revision.output_asset_id`，多存一份就会在回滚版本时对不上。

`GET /assets/{aid}` 出参（`asset` 全字段 + 两个取图地址）：

```json
{ "id": 12, "project_id": 1, "kind": "image", "path": "00_静态资源/01场景-荒郊猪窝-泥潭夜景.png",
  "thumb_path": null, "bytes": 3453715, "w": 1280, "h": 720, "thumb_w": null, "thumb_h": null,
  "dur_s": null, "source": "seed",
  "url": "/api/v1/assets/12/file", "thumb_url": "/api/v1/assets/12/file?thumb=1" }
```

- `w`/`h` 是**原片**尺寸，`thumb_w`/`thumb_h` 是**海报**尺寸（长边 ≤720）。`--thumbs` 抽帧时一起写；没抽过就是 `null`，前端用 CSS `aspect-ratio` 兜底。
- `GET /assets/{id}/file` 支持 `Range`（实测 Starlette `FileResponse` 回 `206` + `Content-Range`），所以 `<video>` 能拖动、能只拉首段。

- `path` / `thumb_path` 是**磁盘相对路径**，只给后端与导入脚本；前端一律用 `url` / `thumb_url`。
- `thumb=1` 时若 `thumb_path` 为空，**回落到原图**并在响应头标 `X-Thumb-Fallback: 1` —— 让前端知道"这张是全尺寸"，而不是拿到 404。
- 缩略图由 `scripts/seed_project.py --load --thumbs` 用 ffmpeg 抽视频首帧生成，落在 `MEDIA_ROOT/.thumbs/<sha前12位>.jpg`（长边 ≤720）。默认关闭，避免每次灌库都重抽；抽帧失败的资产保持 `thumb_path = NULL`，照常回落到原片。实测 19 张共 884 KB、平均 47 KB，单张海报比原片小 **207×**（10.8 MB → 54 KB）。
- 取图必须过两道：项目可读（同 §2.1）+ 路径 `realpath` 后仍在 `MEDIA_ROOT` 内；越界一律 `INTERNAL`（唯一路径遍历面，见 `03` §6.6）。
- 不做独立的 `/media` 静态路由：静态目录会把整个 `MEDIA_ROOT`（真实数据集 566 MB）无鉴权暴露出去，取图走带鉴权的端点。
- **`MEDIA_ROOT` 必须等于 seed 的 `--source` 目录**（`scripts/seed_project.py --source`）。两者不一致时元信息照常返回、取图全部 404 —— 这是最容易踩的一处环境错配。

---

## 4. 错误码总表

`retryable` 指**客户端可原样重试**；`层` 指谁产生。

| code | HTTP | retryable | 层 | 用户文案（`message`） |
|---|---|---|---|---|
| `AUTH_REQUIRED` | 401 | ❌ | api | 请先登录 |
| `AUTH_INVALID` | 401 | ❌ | api | 邮箱或密码错误 |
| `AUTH_DISABLED` | 403 | ❌ | api | 账号已停用 |
| `PERM_DENIED` | 403 | ❌ | api | 无权执行该操作 |
| `PERM_ADMIN_ONLY` | 403 | ❌ | api | 仅超管可操作 |
| `PERM_READONLY_PROJECT` | 403 | ❌ | api | 该项目对你只读 |
| `NOT_FOUND` | 404 | ❌ | api | 资源不存在 |
| `METHOD_NOT_ALLOWED` | 405 | ❌ | api | 不支持的请求方法 |
| `UNSUPPORTED_MEDIA` | 415 | ❌ | api | 不支持的文件类型 |
| `PAYLOAD_TOO_LARGE` | 413 | ❌ | api | 内容超出大小上限 |
| `STALE_VERSION` | 412 | ❌ | api | 内容已被他人修改，请刷新后重试 |
| `CONFLICT_CLIP_INDEX` | 409 | ❌ | service | 该序号已被占用 |
| `CONFLICT_SKILL_ACTIVE` | 409 | ❌ | service | 该技能已有生效版本 |
| `CONFLICT_ASSET_REFERENCED` | 409 | ❌ | service | 该资产正被引用，不能删除（`detail.usages` 列出引用方） |
| `CONFLICT_HAS_REVISIONS` | 409 | ❌ | service | 该内容已有付费产物，删除会让台账漏记（`detail.revisions` 是条数） |
| `CONFLICT_EMAIL` | 409 | ❌ | api | 该邮箱已注册 |
| `LOCK_HELD` | 409 | ❌ | service | 该片段正在被他人编辑 |
| `IDEM_KEY_REUSED` | 409 | ❌ | api | 幂等键已用于不同请求 |
| `VALIDATION_SHAPE` | 422 | ❌ | api | 请求格式不正确 |
| `VALIDATION_RANGE` | 422 | ❌ | api | 参数超出允许范围 |
| `VALIDATION_INVARIANT` | 422 | ❌ | domain | 分镜/参考图/台词不自洽（`detail` 给具体原因） |
| `MODEL_REF_LIMIT` | 422 | ❌ | domain | 该模型最多支持 N 张图 / M 段视频，当前超出（`detail` 带 `kind/used/allowed/model`） |
| `BUDGET_EXCEEDED` | **402** | ❌ | service | 本次生成预算已用尽 |
| `PROJECT_BUSY` | 429 | ✅（带 `Retry-After`） | service | 该项目已有生成任务在跑 |
| `RATE_LIMITED` | 429 | ✅（带 `Retry-After`） | api | 操作过于频繁，请稍后再试 |
| `PROVIDER_RATE_LIMITED` | 429 | ✅ | providers | 上游限流，稍后自动重试 |
| `PROVIDER_TIMEOUT` | 504 | ✅ | providers | 上游超时 |
| `PROVIDER_UNAVAILABLE` | 503 | ✅ | providers | 上游暂不可用 |
| `PROVIDER_DISABLED` | 503 | ❌ | providers | 该通道已被管理员停用（`detail` 带 channel/reason/actor_id/since） |
| `PROVIDER_AUTH` | 502 | ❌ | providers | 上游鉴权失败，请联系管理员检查 Key |
| `ASSET_FETCH_FAILED` | 502 | ❌ | providers | 素材已生成，但下载失败，可重试下载 |
| `CONTENT_REJECTED` | 422 | ❌ | providers | 内容审核未通过，已回退提示词 |
| `JOB_STATE_INVALID` | 409 | ❌ | jobs | 当前状态不允许该操作 |
| `JOB_NOT_CANCELLABLE` | 409 | ❌ | jobs | 任务已结束，无法取消 |
| `ACCEPTANCE_FAIL` | — | ❌ | agent | 验收点连续 3 次未通过 |
| `ESCALATED` | — | ❌ | agent | 无法定位失败原因，需人工介入 |
| `SKILL_TOOL_UNKNOWN` | 422 | ❌ | domain | Skill 引用了未注册的工具 |
| `SKILL_NOT_DRYRUN` | 409 | ❌ | domain | 该版本尚未通过试跑，不能发布 |
| `INTERNAL` | 500 | ❌ | 任意 | 服务内部错误，请附带 request_id 反馈 |
| `INTERNAL_STATE` | 500 | ❌ | 任意 | 状态异常（说明是 bug） |

> **为什么预算用 402**：它是唯一"业务上钱不够"的语义，`422` 会被前端当成表单错误、`403` 会被当成权限问题。402 罕见，正好不会被误分类。前端只需对 402 显示"充值/调低预算"引导。
>
> `ACCEPTANCE_FAIL` / `ESCALATED` 一般**不出现在同步响应里**，只作为 `job.error_code` 的异步终态；HTTP 层没有合适的单一状态码能同时表达"任务失败但系统正常"。前端按 `job.error_code` 分支，不看 HTTP 码。
>
> `INTERNAL_STATE` 单独存在的理由：DB 的 `CHECK` 守卫的是"不该发生的非法状态"，一旦触发说明代码有 bug，必须和普通 500 分开统计。

---

## 5. 关键端点详规

### 5.1 `POST /auth/register` `POST /auth/login`

```json
// 请求
{ "email": "dev@example.com", "password": "至少10位，含字母与数字" }
// 200/201 响应
{ "user": { "id": 3, "email": "dev@example.com", "role": "user", "created_at": "2026-10-18T02:00:00.000Z" } }
```

密码策略：长度 10–72，禁止纯数字；`argon2id` 存 `pwd_hash`。注册 `role` 强制 `user`（不接受客户端传 role）。

### 5.2 `GET /auth/me`

```json
{
  "user": { "id": 3, "email": "dev@example.com", "role": "lead" },
  "capabilities": { "can_create_project": true, "can_write_global": false, "can_write_any_project": false }
}
```

前端**只依赖 `capabilities` 做显隐**，不自行推断角色。`can_write_global` 即 admin 的 skill/provider 权限。

### 5.3 `POST /clips/{cid}/generate`（核心）

```json
// 请求头
Idempotency-Key: gen-clip-77-a1b2c3d4
If-Match: "12"

// 请求体
{ "res": "draft", "budget_limit": "12.000000" }
```

```json
// 202
{
  "job": {
    "id": 512, "kind": "video", "target_kind": "clip", "target_id": 77,
    "state": "waiting_confirm", "attempts": 0, "max_attempts": 3,
    "budget_limit": "12.000000", "cost_used": "0.000000",
    "created_at": "2026-10-18T02:10:00.000Z"
  },
  "preview": {
    "prompt_sha256": "9f2c…", "prompt_head": "将参考图一中的荒郊猪窝…",
    "tokens_est": 4180, "ref_slots": [ { "idx": 1, "asset_id": 41, "role": "实景" } ],
    "cost_est": "1.000000", "duration_s": "5.00"
  }
}
```

- `res=draft` → `seedance-2.0-mini`，**实测只有 720P 一档**；`res=final` → 720P+（若 2.5 系开了更高档）。
  > PRD §8 与 ADR-005 写的是「mini 480P → 720P+」，但 fixture 的 17 条实测全是 `720p`（`02` §5.7 的 key 也是 `seedance-2.0-mini`）。草稿与正片的差价来自**模型档**，不是分辨率 —— 成本估算按模型算，别按 480P 折算。
- 预算只存在 `job` 上（`budget_limit` / `cost_used`）。`project` / `episode` **没有**预算字段，界面不要画"本集剩余"水位；要看预算就看当前 job。
- 默认模式直接进 `waiting_confirm`（付费点），`POST /jobs/{jid}/confirm` 才真正排队。
- 全能模式（`?mode=auto`，P1）跳过 `waiting_confirm`，直接 `queued`，但仍受 `budget_limit` 与 `max_attempts` 约束。
- 幂等重放：同 key 同 body → 200 + 原 job + `Idempotency-Replayed: true`。

**校验顺序**（任一失败即返回；命中幂等键则重放，不新建 job）：

```
1. 鉴权 + 权限（资源归属必须从 id 反查 project，禁止先查资源再判权限）→ 401 / 403 / 404
2. 幂等键命中（`sha256(user_id:key)`）→ 直接重放原 job，**不再往下校验任何一条**
3. If-Match → 412
4. 项目内已有 running 的 video job → 429 PROJECT_BUSY
5. domain 不变量（shots 闭合 / 槽位连续 / 台词逐字内嵌；**槽位数量按 `model_requested` 的容量表校验，降级换模型后要再校验一次**）→ 422 VALIDATION_INVARIANT / 422 MODEL_REF_LIMIT
6. budget_limit ∈ [0.01, 100]，且不超过项目日预算余量 → 422 / 402
7. 落库；`uk_job_idem` 并发冲突时读回首个 job 重放（`03` §8.2）
```

**幂等键必须排在 `If-Match` 之前**：客户端超时重试时，首次请求已经把资源 `version` 推进了，若先验 `If-Match`，重放会永远返回 412 —— 而超时重试正是幂等键唯一的存在理由。命中重放时不校验预算与 domain 不变量，因为它不产生任何新执行。

### 5.3.1 `POST /clips/{cid}/reroll`（打回重发）

**请求头/响应与 §5.3 完全一致**（`I M S` 三件套都在），差别只有两条：

| | `generate` | `reroll` |
|---|---|---|
| 源状态 | `draft` / `prompted` / `review` / `approved` / `rejected` 都能提 | **只能是 `rejected`**，否则 `409 JOB_STATE_INVALID`（先打回） |
| 请求指纹 | `{clip_id,res,budget_limit,mode}` | 多一个 `"reroll": true` |

第二条是必须的：同一个幂等键不能既当"首次生成"又当"打回重发"，否则一次网络重试会把重发悄悄
变成重放（或反之，`409 IDEM_KEY_REUSED`）。

新 take 会让 `revision.parent_revision_id` 指向**上一版**（`clip.current_revision_id`），所以重发链路在
数据里可追 —— 实测 `#37(parent=None) → #38(parent=None) → #39(parent=38)`。

> 前端交互：分镜表里 `review` 的片段有「通过 / 打回」，`rejected` 的片段按钮变成「重发…」并复用
> 同一个付费点弹窗（预估、预算、确认、质检、时间线都不变）。

### 5.4 `GET /jobs/{jid}` 与 `GET /jobs/{jid}/trace`

```json
// GET /jobs/512
{ "id": 512, "kind": "video", "state": "running", "attempts": 1, "max_attempts": 3,
  "budget_limit": "12.000000", "cost_used": "2.270000",
  "provider": "mock", "model": "seedance-2.0-mini", "error_code": null,
  "created_at": "…", "started_at": "…", "finished_at": null }
```

```json
// GET /jobs/512/trace  → 按发生顺序，最多 TRACE_KEEP(20) 条
[
  { "ts": "…", "step": "claim",           "verdict": null,  "ms": null, "detail": { "mode": "auto", "provider": "mock", "attempts": 0 } },
  { "ts": "…", "step": "provider",        "verdict": "ok",  "ms": 90,   "detail": { "model": "seedance-2.0-mini", "cost": "2.270004", "tokens": 260100 } },
  { "ts": "…", "step": "asset",           "verdict": "ok",  "ms": null, "detail": { "asset_id": 62, "path": "第一集/…mp4" } },
  { "ts": "…", "step": "review",          "verdict": "pass","ms": 1594, "detail": { "failed": [], "unknown": ["a3","a5"], "degraded": false } },
  { "ts": "…", "step": "decision",        "verdict": "awaiting_human", "ms": null, "detail": { "clip_status": "review" } }
]
```

时间线存在 `job` 附属表 `job_trace`（`01` §4.11，迁移 `0006`），**每 job 只留最近 20 条**：
它是 job 详情页的面板数据源，不是审计日志。`step` 的取值就这六个：`claim` / `budget_precheck` /
`provider` / `asset` / `review` / `decision`。

> **两个 token 口径不要互相对账**：`trace` 里的 `tokens` 是上游返回的 usage，仅供诊断；
> 台账只认 `revision.input_tokens`（计费口径 = 21,675 × 秒）。同一次调用这两处数字本来就不同源。

> **失败时的锚点**：`run_job` 抛异常会让它此前写的 trace 随 savepoint 回滚，所以 `_fail_job`
> 会在回滚**之后**补一条 `decision: failed` —— 查"为什么失败"必须看这一条。这条是测试
> `test_trace.py::test_failed_provider_is_recorded` 先撞出缺口、再补上的。

### 5.5 `POST /skills/{sid}/versions` `…/dry-run` `…/publish`

```json
// 新建版本（admin）
POST /skills/4/versions
{ "body_md": "## #110 巨物压迫感四件套\n…", "note": "补四件套的反面清单" }
// 201 → draft 版本，服务端解析 sections_json 并算 tokens_est
{ "skill_version": { "id": 88, "skill_id": 4, "version": 7, "state": "draft",
  "checksum": "ab12…", "sections": 12, "tokens_total": 6480 } }
```

```json
// dry-run：只在内存里组装，不发起视频生成、不写 revision、不扣费
POST /skills/4/versions/88/dry-run
{ "clip_id": 77 }
// 200
{
  "injected": [
    { "rule_id": "#110", "title": "巨物压迫感四件套", "tokens_est": 380, "selected": true },
    { "rule_id": "#112", "title": "机位位移＋背景相对位移", "tokens_est": 210, "selected": true },
    { "rule_id": "#117", "title": "文案侧内容安全", "tokens_est": 160, "selected": false }
  ],
  "budget": { "input_limit": 12000, "used_by_skill": 590, "used_total": 4180, "trimmed": false },
  "prompt_preview": "将参考图一中的荒郊猪窝…（截断 2000 字）",
  "diff_vs_active": { "added_rules": ["#117"], "removed_rules": [], "tokens_delta": 160 }
}
```

**试跑目标由「绑定关系」决定，不是由 `skill.stage` 猜**：`skill.name` 唯一（`uk_skill_name`），
所以种子里 `story-to-script` 一行同时服务 S1/S2/S3，`skill.stage` 只是单值标签。服务端按
`StageSpec.skill` 找出绑定到该技能的所有 stage，再用请求给的 id 类型选一个：

| 请求体带 | 注入到 |
|---|---|
| `project_id` | 绑定项目类 stage（S1 / S5） |
| `episode_id` | 绑定集类 stage（S2 / S3；同技能多绑时优先 `skill.stage` 声明的那条） |
| `clip_id` | 绑定片段类 stage（S4） |

三种 id 里有且只有一种，且类型必须落在绑定集合里 —— 给错就 `422 VALIDATION_SHAPE`。
"随便挑一个对象凑合组装"会让预览与真跑的上下文对不上，那比不试跑更糟。
两条例外都是 `422 VALIDATION_RANGE`：`stage=review` 的技能（质检规则只在 jobs 里用），
以及**没有被任何 step 绑定**的技能（例如种子里的 `krea2-character-card`）。

`diff_vs_active` **只比这个技能自己的规则**：预算挤掉别家技能命中的节会在 `budget.trimmed`
里体现，混进 diff 会被误读成"这条规则被删了"。没有 active 版本时基线为空，草稿的规则全算 `added`。

```json
// 发布：把 draft 置 active，旧 active 置 superseded（同一事务）
POST /skills/4/versions/88/publish
// 200
{ "skill": { "id": 4, "active_version": 7 }, "superseded": 6 }
```

规则：**未 dry-run 成功的版本不能发布**（服务端记录 `dry_run_ok_at`，发布时校验）。`body_md` 里若出现脚本围栏（```` ```sh ````/```` ```python ````）直接 `422 SKILL_TOOL_UNKNOWN`。

### 5.6 `GET /revisions/{a}/diff/{b}`

```json
{
  "a": 301, "b": 305,
  "prompt_diff": "--- a\n+++ b\n@@ -3,1 +3,2 @@\n-镜头1（0-3.5秒）：…\n+镜头1（0-3.5秒）：…（补背景相对位移）\n",
  "params_diff": { "res": ["480p", "720p"] },
  "cost_delta": "1.270000",
  "skill_versions_diff": { "seedance-final-prompt": [6, 7] }
}
```

`prompt_diff` 是 **unified diff 文本**，前端用等宽渲染，不自己算 diff。
`params_diff` 的值是各 stage 写进 `params_json` 的**原始 JSON**（可能是字符串、数字、布尔或对象，
例如 S4 的 `prompt` 是对象、steps 的 `skill_sections` 是整数）；服务端只做 before/after 透出，不替调用方猜类型。

### 5.6.1 `POST /clips/{cid}/rollback`

```json
// 回滚到某一次已有产物
POST /clips/77/rollback
{ "revision_id": 301 }
// 200 → ClipOut（片段当前状态）
```

语义（`03` §8.2）：把 `clip.current_revision_id` 指回该 `revision`，并把 `prompt`/`negative` 恢复成**那次实际用的**；
**不写新 revision、不花钱、不产生新产物**。

| 目标 revision 有没有产物 | 状态怎么动 | 为什么 |
|---|---|---|
| 有（真出过片） | `→ review`（已 `approved` 也要回炉） | 审批绑定在具体 take 上（§6.2）：换回旧画面必须重看 |
| 没有（S4 只写了提示词） | **状态不变** | 回滚提示词没有换画面，把 `prompted` 硬推进 `review` 是错的 |

- **幂等**：已经是这一版 → `200` 原样返回，`version` 不推进、不写新 revision；
- `revision` 不属于这个片段 / 不存在 → `404`；`draft` 片段回滚到**有产物**的 take → `409 JOB_STATE_INVALID`（`draft → review` 非法）；
- 重启后 `output_asset_id` 取自 `current_revision_id`，所以镜头墙海报会跟着回滚。

### 5.7 `GET /projects/{pid}/cost`

```
?group_by=model|episode|clip|day&from=2026-10-01&to=2026-10-18
```

```json
{ "items": [ { "key": "seedance-2.0-mini", "calls": 18, "tokens": 1560600, "seconds": "72.00", "cost": "13.620000" } ],
  "total": { "calls": 24, "cost": "15.880000" } }
```

数据源只有 `revision.cost`，**不重算**。

- `group_by=episode` 的 key 是给人读的 **「第N集」**（真实双集实测：`第1集 22 次 ¥50.782519 / 第2集 5 次 ¥2.648338`）——
  多集项目最常问的就是"这一集花了多少"，前端不该自己按 clip 加总。
- `group_by=clip` 的 key 是 `clip:{id}`；`stage` 仍不支持（阶段还没落到 `revision` 上），传了返回 `422`。
- `from`/`to` 同时给时，跨度超过 `TIME_RANGE_MAX_DAYS`（366 天）→ `422 VALIDATION_RANGE`（防全表扫描）。

### 5.8 `GET /admin/providers` / `POST /admin/providers/check` / `POST /admin/providers/{channel}/state`

```json
// GET → 200（admin）
{
  "provider": "openai",
  "base_url": "https://api.example.com/v1",
  "text":   { "model": "text-model",   "real": true },
  "vision": { "model": "vision-model", "real": true },
  "video":  { "model": "seedance-2.0-mini", "real": false },
  "timeout_s": 120,
  "note": "配置来自环境变量，进程内不可改；换了 key 要重启。…"
}
```

```json
// POST /admin/providers/check → 200 真打一次上游（最小请求）
{ "ok": false, "provider": "openai", "model": "text-model", "latency_ms": 812,
  "detail": "上游鉴权失败（检查 PROVIDER_API_KEY）（retryable=False）" }
```

**没有 PATCH**：provider 配置来自环境变量，进程启动即固定。面板上放一个改不动的开关比没有更危险
（`03` §12.2 的"不留占位实现"）。`video.real` 现在恒为 `false` —— 真视频厂商的接口形态差异太大
（task 型 / 回调型 / 直出二进制），接的时候按厂商新增一个适配类，不猜。

#### provider 适配层（`00` §3.1）的状态

| 能力 | mock（默认） | openai（真） | ark（真 · Seedance） |
|---|---|---|---|
| `text()` S1–S5 | 按 stage 回放固定结构 + 上下文指纹 | `/chat/completions` + `response_format=json_object`，**并把 pydantic schema 作为输出契约**塞进 prompt（不给契约模型会自己包信封，实测） | — （只做视频） |
| `vision()` 内容级验收 | 预设表回放，evidence 带 `[mock]` | 联络图以 data URL 送多模态模型（实测 `deepseek-flash` 可判），逐条判 `pass/fail/unknown`，越协议的 verdict 一律丢弃 | — |
| `video()` 出片 | 回放真实成片，成本按实测常数算 | 明确抛 `ProviderError` | `/contents/generations/tasks` 建任务 + 轮询 + 下载到 `MEDIA_ROOT/.gen/` |
| `image()` 出图 | 复制一张已有素材（`source="mock"`），链路照跑 | 明确抛 `ProviderError` | `/v1/images/generations`，兼容 `b64_json` 与 `url` 两种返回，落 `.gen/img-<sha>.png` |
| 计费 | 12 s × 0.189167 元/秒 | text/vision 的 `cost` 记 0、`unit_price=None`（文本单价未实测） | **按 token**：`usage.completion_tokens ÷ 1000 × 元/千token`，`unit_price=None`（元/秒是伪口径） |

**两条通道要分开取 provider**：`video()` 用 `PROVIDER_VIDEO`（job 的 `provider` 字段），
`text()`/`vision()` 用 `PROVIDER`。视觉判定曾经复用执行出片的那个 provider，于是"配了真视觉却还在
用 mock 判"—— 静默退化，只有真跑才看得见。同一个坑还有一次：`make_provider(settings)` 的形参默认值
写成 `"mock"`（truthy），把 `settings.provider` 直接吃掉；默认值必须是 `None`。

真机实测（2026-10-19）：视觉 `deepseek-flash` 判 6 帧联络图 13.2 s / 639 tokens，evidence 例如
「六帧画面中均未见烧入字幕、平台水印或logo；第三、四帧的"99%"与图标属于剧情内系统界面元素」。

真机实测（2026-10-19，2.0mini / 480p / 4 s / 含音轨）：创建 → 轮询 8 次（约 90 s）→ `succeeded`；
`usage.completion_tokens = 40_594`，按 `PROVIDER_VIDEO_PRICE_PER_1K_TOKENS=0.008727` 折 **0.354264 元**；
产物 864×496 / 4.10 s / 1.59 MB，签名直链 24 h 有效。

**真出片必须补元信息**：入仓时 `_resolve_output` 调 `seed.enrich_asset()` 补宽高/时长/海报，
否则镜头墙 `?thumb=1` 直接 404 —— 这是接真视频通道才暴露的缺口（mock 走 seed 早补过了）。

错误分类（测试逐条钉住）：401/403/400 → 不可重试；429/5xx/超时/连接失败/任务 `failed` → 后者不可重试、
其余可重试，`Retry-After` 原样带出。

### 5.10 `POST /projects/{pid}/images`（出图）

```json
POST /projects/1/images
{ "prompt": "白胡子师父，黑色滚金边道袍，全身正面，纯色浅灰背景，无文字无水印" }
// 201
{ "asset_id": 85, "path": ".gen/img-4fad5e5a9b3d4951.png", "source": "gen",
  "provider_source": "gen", "deduped": false,
  "model": "gpt-image-2", "cost": "0.1", "latency_ms": 34865,
  "url": "/api/v1/assets/85/file", "thumb_url": "/api/v1/assets/85/file?thumb=1" }
```

- **同步返回**，不排 job：出图 10~40 s，没有"付费点确认"这一步，排 job 只是多一层轮询。
- 产物走和出片同一条入仓路径：落 `MEDIA_ROOT/.gen/` → 建 `asset`（`enrich_asset` 补宽高与海报）→
  之后**槽位指派**就能直接引用它（`GET /assets?project_id&kind=image` 里能看到）。
- 通道由 `PROVIDER_IMAGE` 决定：`mock` = **指向**已有素材（不复制文件，零成本，链路照跑）；
  `images` = 任何 OpenAI Images 兼容的 `/v1/images/generations`（`gpt-image-2` / `gpt-image-2.5-*`）。
- 真机实测：`gpt-image-2` / `low` / 1024×1024 → 40 s、1.58 MB PNG、1024×1024、缩略图 53 KB。
- `deduped=true` 表示按 `sha256` 命中了项目里已有的资产（此时 `source` 可能是 `seed`/`upload`），
  **这次到底是不是真出图看 `provider_source`** —— 前端就靠它标「真出图 / mock 回放」。
- `asset.source` 只有 `gen`/`upload`/`seed` 三态（`01` §4.5 的 CHECK），所以 mock 回放记 `seed`；
  `path` 一律存正斜杠（Windows 上 `Path` 会给反斜杠，混着存前后端要兼容两套）。

**前端**：流水线 S5 卡片跑完后有「用人物卡出图」，把 characters 的 `name：anchor` 拼成 prompt 直接出图并回显。

### 5.9 `POST /locks/acquire` / `POST /presence/heartbeat`
```json
// acquire → 200 { "lock": { "target_kind":"clip","target_id":77,"user_id":3,"expires_at":"…" } }
//         → 409 LOCK_HELD { "error": { ..., "detail": { "holder": "李四", "expires_at": "…" } } }
// heartbeat 请求
{ "project_id": 2, "focus_target": "clip:77" }
// 200 { "online": [ { "user_id": 3, "email": "a@x.com", "focus_target": "clip:77" } ] }
```

**锁只影响 UI 提示，任何写请求都不得因锁失败**（`00` §3.9）。

---

## 6. 状态机（前端据此渲染按钮，后端据此拒绝）

### 6.1 `job`

```
                 ┌─────────────── cancel ──────────────┐
queued ──claim──▶ running ──ok──▶ done                 ▼
  ▲                 │  │             cancelled
  │                 │  └─need_confirm─▶ waiting_confirm ──confirm──▶ queued
  │                 └──fail──▶ failed ──retry──▶ queued
  │                            │
  └── budget_hit ──▶ paused_budget ──(提高预算后 retry)──▶ queued
```

| 迁移 | 允许的源状态 | 触发 | 非法时错误码 |
|---|---|---|---|
| `queued → running` | queued | worker claim | — |
| `running → done` | running | 全部验收点 pass | — |
| `running → failed` | running | 重试耗尽 / 不可重试错误 | — |
| `running → paused_budget` | running | 预算穿线 | — |
| `queued/running → cancelled` | queued, running, waiting_confirm | 用户取消 | `JOB_NOT_CANCELLABLE` |
| `waiting_confirm → queued` | waiting_confirm | `confirm` | `JOB_STATE_INVALID` |
| `failed/paused_budget → queued` | failed, paused_budget | `retry`（`attempts+1`） | `JOB_STATE_INVALID` |

`done` / `cancelled` 为终态，不可再迁移。

### 6.2 `clip`

```
draft ──S4──▶ prompted ──generate──▶ generating ──▶ `review ──pass──▶ approved ──新 take──▶ review`（审批绑定在某一次产物上：重跑出片必须回炉）
                                              │        ├──fail──▶ rejected ──reroll──▶ generating
                                              │        └──打回（approved 也可直达）──▶ rejected
                                              └──cancel──▶ prompted
```

`approved → rejected` 是刻意留的捷径：**通过之后又发现问题**时，"撤销通过并打回"一步到位，
否则得 `approved → review` 再 `review → rejected` 两次点击（中间还会闪一下待审状态）。

### 6.3.1 数值化质检门

`acceptance_result` 的每条判据可带 `score`（0–100，`unknown` 不带分）。动线判定只能给粗分
（pass=90 / fail=30，它不知道画面内容）；视觉判定由模型给分。所有已判定判据的均分低于
`VISION_SCORE_GATE=70` 时：

- **默认模式**：不自动放行，片段留在 `review`，trace 记 `decision=low_score`（附 score/threshold）；
- **自动模式**：写一条 `low_score` 教训、把修法追加进提示词、`attempts+1` 后重排（仍受 ≤3 与预算前置约束）。

分数同时写入 `job_trace` 的 `review` 与 `decision` 两步，前端据此显示「均分 / 阈值」。

### 6.3 `skill_version`

```
draft ──dry-run ok──▶ draft(可发布) ──publish──▶ active ──新版本 publish──▶ superseded
```

`superseded` 可通过 `POST /skills/{sid}/rollback {version}` 复归 `active`（原 active 变 `superseded`）。

---

## 7. SSE 协议

```
GET /api/v1/projects/{pid}/events
Accept: text/event-stream
```

### 7.1 帧格式

```
id: 1024
event: job.updated
data: {"seq":1024,"ts":"2026-10-18T02:11:03.120Z","job":{"id":512,"state":"running","cost_used":"2.270000"}}

: ping
```

| 帧 | 频率/条件 |
|---|---|
| 业务事件 | 状态变化即时推送 |
| `: ping` 注释帧 | 每 15 s，防中间层超时断连 |
| 连接时先补发 | 最近 100 条事件（内存环形缓冲），保证前端刷新后自洽 |

### 7.2 事件类型

| event | 触发 | data 关键字段 |
|---|---|---|
| `job.updated` | job 状态/成本/attempts 变化 | `job{id,state,attempts,cost_used}` |
| `job.trace` | 新增一条 trace | `job_id, step, ms, cost` |
| `clip.updated` | clip 内容/状态变化 | `clip{id,status,version}` |
| `cost.updated` | 有新的 `revision.cost` | `project_id, day_cost, total_cost` |
| `presence.updated` | 有人上线/下线/切换焦点 | `online[]` |
| `lock.updated` | 锁获取/释放/过期 | `target_kind,target_id,holder` |
| `skill.published` | Skill 新版本生效 | `skill_id, active_version` |
| `provider.updated` | provider 熔断/恢复/被停用 | `name, healthy, cooldown_until` |

### 7.3 断线续传

- 浏览器 `EventSource` 自动带 `Last-Event-ID`；服务端从环形缓冲补发 `seq > Last-Event-ID` 的事件。
- 缓冲不足（服务重启）→ 服务端发 `event: resync`，前端据此**全量刷新当前视图**（不尝试增量重建）。
- **SSE 连接不占用 DB 连接**：事件从进程内 `asyncio.Queue` 出，落库由 service 另做。

### 7.4 实现说明（骨架 v1 已落地）

| 项 | 实现 |
|---|---|
| 总线 | `manju/events.py`：每项目一份 `deque(maxlen=SSE_BUFFER)` + 每个订阅者一个 `asyncio.Queue`；`publish()` 通过 `loop.call_soon_threadsafe` 投递（worker 跑在 `to_thread` 里，这个跨线程投递是必须的） |
| 事件源 | `job.updated`（submit/confirm/claim/retry/done/failed/paused/cancel）、`clip.updated`（状态变化）、`cost.updated`（新 revision 已落账） |
| 鉴权 | 单独开短 session 查一次权限就关；**不挂 `get_session` 依赖**，否则一条 DB 连接会被流钉住 |
| 上限 | 每用户 `SSE_MAX_CONNECTIONS_PER_USER`（5）条连接，超了 `429 RATE_LIMITED`；慢客户端满队丢最老的，靠 `Last-Event-ID` 补 |
| 前端 | `EventSource` 订阅 → 失效对应查询；断线由浏览器自动重连，**轮询仍保留作兜底**（事件丢了也不会看到过期数据） |
| 测试 | 流用 `event_stream()` async generator 直接驱动（12 条用例）；不用 `TestClient` 读无终止响应 —— 那会把响应缓冲到超时，是测法错误 |

> 心跳 `: ping` 每 `SSE_PING`（15 s）一次，同时用作断连探测（`request.is_disconnected()`）。

---

## 8. 前端消费约定

| 场景 | 做法 |
|---|---|
| 列表/详情 | TanStack Query，`staleTime: 0`，`refetchOnWindowFocus: true` |
| job 进度 | **优先 SSE**；SSE 不可用时降级为 `GET /jobs/{jid}` 轮询，间隔 2 s→5 s 退避 |
| 写操作 | 一律 `invalidateQueries([...])`，不手动改 cache（避免与 SSE 打架） |
| `If-Match` | 从最近一次 GET 的 `ETag` 取，存在 mutation 的 `variables` 里；`412` 时提示"已刷新"并自动 `refetch` |
| 幂等键 | mutation 时生成一次（`crypto.randomUUID()`），挂在 `mutationKey` 上，重试复用同一个 |
| 错误提示 | 按错误码分级：`401` → 跳登录；`402` → 引导调预算；`403` → toast；`409/412` → 就地提示 + 刷新；`429` → 按 `Retry-After` 倒计时，到期自动重试；`502 ASSET_FETCH_FAILED` → 提供"重试下载"按钮（不要引导重新生成，那是二次扣费）；其余 `5xx` → 显示 `request_id` 并提供复制 |
| 长文本 | 提示词一律虚拟滚动 + 等宽字体；不做富文本 |

---

## 9. 兼容规则

1. **加字段**：允许，前端必须忽略未知字段。
2. **删字段 / 改语义 / 改错误码名**：破坏性，需同步改本文 + PRD 变更记录。
3. 新增端点不需版本号；删除端点需一个 minor 版本的双写期（v0 不承诺，因为单前端）。
4. `detail` 内的结构**不承诺稳定**，前端只对 `code` 分支。

---

## 10. 待定（v0.4 收口）

- ~~`job.trace` 的落库位置（`job` 附表 vs `revision` 冗余）需在 W2 定。~~ → 已定：独立附表 `job_trace`（`01` §4.11，迁移 `0006`），每 job 留 20 条。
- provider 路由表配置的对外只读接口（是否允许 lead 查看当前 model 映射）。
- 批量生成（一次提多个 clip）暂不做：与"逐 clip 确认"的产品语义冲突，v0 明确只支持单 clip 提交。

---

### 5.11 golden 回归集（20 条契约测试）

`apps/api/tests/test_golden_regression.py`：改 Skill / 模板 / 状态机之后跑它，判断有没有把别人弄坏。
A 组 5 条（S1–S5 写回与状态推进）、B 组 7 条（`01` §5.4 的域不变量必须拦住坏数据）、
C 组 8 条（幂等 / If-Match / 预算 0 / reroll 前置状态 / 未试跑禁发布 / 回滚幂等 / 集重排 / trace 上限）。
**只断言契约，不断言模型措辞** —— 所以 mock provider 也能跑，零成本可重复。

## 11. 实现状态（骨架 v1）

按 `03` §12.2 的原则：**没实现的端点就是不存在**（404），不留占位实现。

| 组 | 状态 |
|---|---|
| `/auth/*`、`/projects*`、`/episodes*`、`/clips*`、`/revisions*`、`/assets/*`、`/cost`、`/lessons`、`/locks*`、`/presence`、`/health`、`/clips/{cid}/rollback` | ✅ 已实现 |
| `/clips/{cid}/generate`、`/jobs*` | ✅ 已实现（`MockProvider`，零成本回放真实 fixture；内嵌 worker 每 2 s 捞一次） |
| `/steps/templates`、`/steps/S1..S5/run`、`/skills*` | ✅ 已实现（B 阶段只读 + P1-1 写路径：新建版本 / dry-run / publish / rollback / PATCH） |
| 出图通道 `POST /projects/{pid}/images`（S5 人物卡 → 真出图 → 进资产库） | ✅ 已实现（`PROVIDER_IMAGE=images` 实测 gpt-image-2） |
| 前端「流水线」页（P0-3）、「版本链」面板（P0-8）、「Skill 维护」页（P1-1）、job 时间线（P1-4） | ✅ 已实现（五张 stage 卡 + 新建项目/分集；take 列表、回滚、两版 diff；Skill 新建版本、按绑定选 stage 试跑、发布、回滚、启停；`claim→provider→asset→review→decision` 时间线） |
| `/clips/{cid}/reroll` | ✅ 已实现（源状态必须是 `rejected`，否则 `409 JOB_STATE_INVALID`；与 `generate` 共用提交路径，指纹带 `reroll`） |
| `/projects/{pid}/events`（SSE） | ✅ 已实现（事件总线 + `Last-Event-ID` 补发 + `resync` + 每用户连接上限） |
| `/admin/providers` | ✅ 配置视图 + 连通性自检 + **运行时开关**（`runtime_flag` 表，多副本共享；配置本身仍来自环境变量、进程内不可改） |

已知取舍：

1. `cost` 的 `group_by` 支持 `model` / `day` / `clip`；`stage` 暂不支持——pipeline 阶段还没落到 `revision` 上，等 steps 落地后再开（传 `stage` 会按枚举校验返回 422，而不是静默当成 `model`）。
2. `PATCH /clips` 可以改 `status`，但只能沿 §6.2 的迁移图走（非法跳转 → `409 JOB_STATE_INVALID`，同状态视为幂等）。内容与状态一起改时，先判迁移再判 `01` §5.4 的不变量。
3. 登录失败限流用进程内计数（`03` §12.4 白名单内），多实例部署前要挪到共享存储。
4. **拿人验权限时用对账号**：`admin@example.com` 对所有项目只读，`generate` 会回 403 —— 这是设计，不是 bug。可写账号见 §1 的演示账号表。
5. **`steps/*/run` 不带幂等键**（见 §3 顶部偏差说明）；**S3 重跑会 `409 CONFLICT_CLIP_INDEX`** —— 该集已有片段时请新建一集，避免把已生成的内容冲掉。
6. **S5 只产出不落库**：人物卡最终要生成的是图，而 `provider.image` 还没实现；角色卡在响应与 `revision.params_json` 里，等资产库落地再写进 `asset`。
7. **生成后自动质检**：`running → done` 之前会跑 `video-motion-review`（真调 `vscan.ps1`），逐条验收点写成 `pass` / `fail` / **`unknown`** 落 `revision.acceptance_result_json`，动线落 `motion_json`。
8. **`unknown` 不等于通过**：只有**全部**判据都真判过才置 `clip.status=approved`。动线判不了的内容级判据会再交视觉模型；视觉也判不了（或 `VISION=0`）就留在 `review` 等人看。
9. **`?mode=auto` 才会自动重试**：失败且归因可执行时写 `lesson`、把 `fix` 追加进 `clip.prompt`、同一 job `attempts+1` 重排（≤3，预算前置）；归因不出来 → `failed + ESCALATED`，用尽 → `failed + ACCEPTANCE_FAIL`。`mode` 落在 `job.mode` 上。
10. **质检可降级**：`VSCAN_PATH` 指向 `vscan.ps1`；脚本/PowerShell 缺失或解析失败时 `degraded=true`、判据全 `unknown`，链路继续跑（开源仓库不能要求别人先装仓库外的脚本）。视觉判定的降级见 `03` §7.4。
11. **视觉判定现在是 `mock` 预设表**：抽帧（`ffmpeg` 拼 3×2 联络图）与合并逻辑都是真的，但 `MockProvider.vision()` 读不了图，只能按判据关键词回放预设结论，每条 `evidence` 都带 `[mock]` 前缀。接真视觉模型时只需实现一个 `Provider.vision()`，上层不改 —— 在那之前，"通过了"这个结论只对链路有意义，对画面没有。
12. **Skill 写路径是单向门**：`draft → 试跑（记 dry_run_ok_at）→ publish`，没试跑过的版本发布不了；`publish` 对已 `active` 的版本幂等（200，`superseded: null`），`rollback` 才允许 `superseded → active`。试跑与真跑共用 `steps._build`，注入结果不可能不一致。
13. **片段回滚分两支**（§5.6.1）：目标 revision 带产物 → `clip.status` 回 `review`（换了画面必须重审）；不带产物（S4 只写提示词）→ 只恢复 `prompt`/`negative`，状态不动。两支都幂等、都不写新 revision。`test_clip_rollback.py` 逐条钉住。
14. **P0-3 的界面是本轮补的**：五张 stage 卡（S1/S5 打项目、S2/S3 打集、S4 打片段）+ 新建项目/分集，DoD 的"空白项目 → 有分镜表"现在全程可以只用界面完成；S3 遇到已有片段会 409，界面把这条提示写在流水线页脚。

### 11.1 三个刻意的判断（面试可直接讲）

这三条都是"实现上更省事、但会让结论变假"的地方，故意选了更麻烦的那边：

| 判断 | 为什么 | 代价 | 在哪验证 |
|---|---|---|---|
| **`unknown` 不算通过** | 动线只能判"画面在不在动"，判不了"功德环在不在"。把 `unknown` 当通过，等于拿运动数据给画面内容背书 —— 结论是假的 | 内容级判据只有视觉模型真判过才算数；判定不了就停在 `review`（`VISION=0` 时全项目都看不到自动 `approved`） | `test_review.py::test_judge_with_no_judgeable_criteria_stays_unknown`、`test_review_vision.py::test_vision_keeps_unknown_without_preset` |
| **自动重试只对 `?mode=auto`** | 默认模式的产品语义是"付费点停下等人确认"，自动改词重发会绕过人的决定 | 默认模式失败即 `failed`，不写 `lesson`、不改措辞 | `test_review.py::test_default_mode_fails_without_retry` |
| **vscan 缺失时降级不报错** | 开源仓库不能要求使用者先装仓库外的脚本 + ffmpeg，硬依赖就是"clone 下来跑不起来" | 降级时判据全 `unknown`，看起来"什么都没验"，但 `degraded=true` 说得很清楚 | `test_review.py::test_scan_degrades_without_script` |

重试的三条停止判据一并钉死（`03` §4.1）：`attempts ≤ 3`、`cost_used ≤ budget_limit`（**每次调用前查**）、
归因不出可执行改动就 `ESCALATED` 转人工。缺任何一条，Agent 都会在"同一句改写重复三次、烧完预算"上翻车。

**空数据语义**（前端清单 §2.2 #7 的约定，后端配合）：字段有值就是值；字段为 `null` 或数组为空就是**没有数据**（UI 用灰 `—`）；端点整体不存在才是**未接入**（UI 用琥珀「待接入」）。两者不能混用 —— 把 `null` 画成 "0" 或把 404 画成 "无数据" 都会误导评审。

`presence.online[]` 只有 `user_id / email / focus_target`，没有 `name` / `avatar_url`：`user` 表本来就没有姓名字段，头像也没有存储位置。前端用邮箱首字母（原型已这么做），需要真名时再加列。


### 7.5 SSE 跨副本（`event_log`，迁移 `0008` + `0009` 去 FK）

- **两条来源，按 `seq` 去重**：本副本订阅队列（毫秒级直推）+ `event_log` 轮询（跨副本兜底，周期 `SSE_POLL=0.5s`）；
- `id:` 一律是 `event_log.id`（全局自增），所以 `Last-Event-ID` 换到**任意副本**都能续传；
- 日志缺口（清理掉了 `after` 之后那段）→ 发 `resync`，前端全量刷新而不是增量重建；
- **代价**：跨副本延迟上限 ≈ 一个 `SSE_POLL`；单副本仍是实时直推，不受影响；
- **不占 DB 连接**：流本身不挂 `get_session`；落库与轮询都是短会话（一次性开、用完关）；
- **发布不阻塞请求**：`event_log` 没有 `project` 外键 —— 否则 `submit`/`confirm` 持有的行锁会和 FK 校验自锁（实测 30 s）。

真机验证（两个 uvicorn 进程共享 `manju_dev`）：B 副本提交并确认一次生成 → A 副本的 SSE 流收到
`id: 6 / 7 event: job.updated`，证明事件不是从进程内总线来的。
