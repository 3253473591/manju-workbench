# 验收记录

对照 [PRD §9 验收](baseline/PRD.md) 逐条核对。**只写有证据的结论**：跑过的写通过，没跑过的写未验收，
不用 mock 冒充 real。

需求追溯（PRD 条款 → 手册条款 → 代码入口 → 测试 → 证据）见
[`requirements-traceability.md`](requirements-traceability.md)。

**本文只写「在什么环境下、用哪条命令、跑出什么结果」**。结论都限定在这一组条件下，
换环境（不同 `DATABASE_URL`、并发跑同一套测试、库没迁移完）都可能不成立。

| 项 | 结论 | 复现命令 | 条件 |
|---|---|---|---|
| 后端测试 | **117 passed** | `python -m pytest`（见 §0 的环境要求） | MySQL 8；库名 `*_test`；单会话独占 |
| 迁移往返 | `base → head` 通过 | `python -m alembic -c alembic.ini downgrade base && python -m alembic -c alembic.ini upgrade head` | 目标库为 `manju_director_test` |
| 迁移漂移 | `No new upgrade operations detected` | `python -m alembic -c alembic.ini check` | 同上（对**迁移到 head 的库**跑；对遗留演示库跑会报历史漂移） |
| 接口契约 | 55 路径 / 70 操作 / 95 schema 与服务端一致 | `python scripts/export_openapi.py` + `python scripts/validate_openapi.py` + `pytest tests/test_openapi_conformance.py` | 无外部依赖 |
| 前端构建 | `tsc -b && vite build` 通过 | `cd frontend && npm run build` | 只证明可构建，不证明后端闭环 |
| 并发运行 | 两个 pytest 会话同时跑同一测试库：串行化，各自全绿 | 见 §0.2 | 依赖会话级 `GET_LOCK` |

- 截图：`docs/screenshots/`，由 `scripts/shot.py` 通过 Chrome CDP 抓取

---

## 0. 跑测试的环境要求（不满足会静默给出错误结论）

### 0.1 测试库是强制指定的

`backend/tests/conftest.py` **强制**把 `DATABASE_URL` 指到测试库（不再用 `setdefault`），
因为用例会 `drop_all()` / `TRUNCATE` 目标库：

- 想换库：设 `MANJU_TEST_DATABASE_URL`，**不要**靠 `DATABASE_URL`（那份是给应用跑的）；
- 库名必须以 `_test` 结尾，否则直接拒绝运行（`pytest.exit`）——防止误伤演示库；
- 迁移后校验表齐全，缺表立即报错，而不是等用例随机 500。

```powershell
# 当前目录：<仓库>/backend
$env:MANJU_TEST_DATABASE_URL = "mysql+pymysql://<user>:<password>@127.0.0.1:3306/manju_director_test?charset=utf8mb4"
$env:PYTHONPATH="$PWD\.pylibs;$PWD"
$env:PYTHONUTF8='1'
python -m pytest
```

> 不要再加 `-q`：`pytest.ini` 的 `addopts` 已经有 `-q`，再加一层会把结尾的 `117 passed` 汇总行压掉。

版本与基础设施（Python 3.13.x / Node 24.x / MySQL 8.0 / ffmpeg）先用
`python backend\scripts\check_env.py` 过一遍；依赖按 `backend/requirements.lock` 锁定安装。
一条命令核对全部结论：`.\scripts\verify_clean_env.ps1`（见 [README §3](../README.md)）。

> 之前的版本这里是 `os.environ.setdefault("DATABASE_URL", ...)`。只要你的 shell 里
> 导出过 `DATABASE_URL`（例如刚在同一个窗口跑过后端），整轮测试就会悄悄打到**演示库**，
> 而且和正在运行的 dev server 抢同一批表——表现是
> `Table 'xxx.clips' doesn't exist`、`Table definition has changed`（MySQL 1412）、
> `Deadlock found`，以及权限/项目/结果节点/reconciler/幂等等用例随机失败。
> 这不是「用例不稳定」，是测试环境没隔离。

### 0.2 并发跑同一套测试会被串行化

`prepared_database` 会先取一把 MySQL 命名锁（`GET_LOCK`），整个会话持有，结束才释放：

- 两个 pytest 会话同时跑 → 后者等待，前者跑完再开始（不再互相 TRUNCATE 与迁移）；
- 等满 `1800s` 还没拿到锁 → 明确报错并提示换库，而不是继续跑出随机失败；
- 进程被杀时 MySQL 会自动释放该连接上的锁，不会留下死锁。

**不要**用 `DATABASE_URL` 指到演示库来「共享」测试数据。要看演示数据请在浏览器里看。

---

## 1. 新建项目，填写大纲，生成人物设定并保存 — ✅

- `POST /projects` 在同一事务内创建项目、第 1 集、大纲文档与居中的大纲节点
  （`project_service.create_project`；`test_projects.py::test_create_project_bootstraps_episode_and_outline_node`）
- 大纲草稿 → 保存为 v1（`test_content_versions.py`）
- 「人物设定」动作把结果写进配套设定文档的**草稿区**，不改写旧版本
  （`text_ops.handle_setting_generate`）
- 证据截图：`03-workspace.png`（抽屉里可见大纲 v3 与专用按钮）

## 2. 生成剧本，拆出 2–3 个 Clip，能追溯剧本和设定快照 — ✅

- 设定为空时「生成剧本」返回 422（`BUSINESS_RULE_VIOLATION`）
- 生成剧本时把「大纲正文 + 人物设定」一起保存成不可变版本，并写入
  `boundOutlineRevisionId` / `boundSettingRevisionId`
  （`test_mock_chain.py::test_full_mock_chain` 断言两者非空）
- `split-clips` 拆出 3 个独立 Clip 节点，每个 Clip 带内部镜头（编号、起止时间、画面、
  景别运镜、对白音效），由 `clip_shots` 承载，不另建节点

## 3. 确认资产清单，生成或导入图片；同名已有资产能复用 — ✅

- `plan-characters` 只产出清单，**不直接生图**
- `POST /asset-plans/confirm`：命中已有结果/方案时复用，未命中才创建图片方案节点及初始 V1，
  并把规范资产名回填到 Clip 参考列表（`asset_plan.confirm_plan`）
- `test_mock_chain.py` 断言 `createdMediaNodes` 与 `references` 均非空
- 同名查重键为 `(project_id, asset_type, canonical_name, variant_name)`，DB 层唯一约束

## 4. 从立绘联合派生两张图；更换立绘再次操作创建新结果，旧源图记录不变 — ✅

- `POST /media-nodes/{id}/derive` 固定新建 2 个结果节点、各生成 1 张
- `test_acceptance_extra.py::test_derive_twice_creates_new_results_and_keeps_old_source`：
  两批派生节点互不相同；**旧批次的节点内容与产物 id 一字未改**；两批各自记着自己的源图

## 5. 屏蔽多余图片候选，唯一有效结果才通过视频方案前置校验 — ✅

- 屏蔽只影响当前节点，不级联、不删媒体：`PATCH /media-nodes/{id}`
- 视频方案前置校验要求每项资产**恰有一个**未屏蔽、文件可解码的图片结果；
  0 个 / 多于 1 个 / 文件不可用都返回 422 `ASSET_REFERENCE_INVALID` 并**点名具体资产**，
  且**不创建任何方案节点、不调用视频模型**
  （`test_mock_chain.py::test_video_plan_requires_exactly_one_usable_result` 断言节点数不变）
- `test_full_mock_chain` 中屏蔽到只剩 1 个后，`usableResultCount == 1` 才通过

## 6. 生成视频方案、选版、主动抽卡；视频落盘后可播放并展示首帧 — ✅

- `POST /clips/{id}/video-plans` 冻结 Clip、参考图与槽位顺序，再生成初始方案
- `test_full_mock_chain`：`referenceImages` 数量为 1、`frozenClip.id` 与 Clip 一致；
  抽卡后 `media.decodeStatus == verified`、`durationMs > 0`、`thumbnailUrl` 非空；
  `GET /media/{id}/file` 与 `/thumbnail` 都返回 200 且首帧是 `image/*`
- 真实视频同样验证过：`docs/real-verification.md`（mp4 1280×720 / 5088ms / 首帧提取成功）

## 7. 修改 Clip 再生成得到新方案节点，旧方案重抽仍使用旧上下文 — ✅

- `test_acceptance_extra.py::test_new_video_plan_does_not_rewrite_old_frozen_context`
- **这条测试抓到过一个真实缺陷**：方案详情的 `frozenClip` 原本读的是实时 Clip 行，
  改过 Clip 之后旧方案会显示新标题——等于偷偷读了最新 Clip。
  已改为从输入快照读取（`media_service.get_media_node_detail`）。

## 8. 查看版本和来源；manager/admin 可看费用，user 不能读取计费或隐藏 A — ✅

- `test_permissions.py`：三角色对项目/文档/计费/用户管理的读写矩阵逐项断言；
  manager 的所有写操作 403，但 GET 正常
- `test_full_mock_chain`：user 拿到的 attempt `usage` 为 `null`
- 隐藏 A 与 `final_input_text` 只在服务端保存，任何响应模型都不含这两个字段
  （`prompt_version_out` 只暴露 `hiddenTemplateVersion` 与 `finalInputHash`）
- `test_real_mode_guard.py::test_mock_and_real_are_distinguishable`：mock attempt 必带 `isMock`

## 9. 刷新后节点、版本、布局、引用及已交付媒体仍可读取 — ✅

- 全部状态在 MySQL 持久化，无内存态
- `test_full_mock_chain` 末尾重新拉画布，断言节点、布局宽高、依赖边仍在
- `test_canvas.py::test_layout_save_does_not_touch_content`：保存布局不改变文档 `version`
  与节点内容指纹
- 媒体以文件形式落在 `var/media/`，`file_key` 用 ULID，不与业务名冲突

## 10. mock 跑通完整链路且无外部模型请求；real 至少验证一个 Clip 从文本到实际图片、视频 — ✅

**mock（通过）**

- `test_mock_chain.py::test_full_mock_chain` 一次跑完
  大纲 → 设定 → 剧本 → 3 Clip → 资产规划 → 确认 → 出图 → 联合派生 → 屏蔽 → 视频方案 → 视频
- `APP_MODE=mock` 时 `registry` 只返回 `Mock*Provider`，构造不出任何 HTTP 客户端
- mock 产物是**真的**：图片用 Pillow 生成并通过解码核验，视频用 ffmpeg 编码成可播放 MP4 并抽出首帧

**real（完整链路已跑通，14 个 attempt 全部真实调用）**

项目 `01M2ZHP6Z36RPS24D3Y8HQ21Y3`：

| 步骤 | 耗时 | 输出用量 |
|---|---:|---:|
| 人物设定 | 82s | 4,297 |
| 生成剧本 | 155s | 7,352 |
| 分镜拆分（5 个 Clip） | 188s | 12,678 |
| 人物立绘规划（林晚 / 沈知夏） | 71s | 3,456 |
| 出图 ×2 | 20s / 22s | 14,400 each |
| 立绘联合派生 ×2 | 59s / 67s | 14,400 ×2 |
| 视频方案 | 57s | 2,772 |
| 视频抽卡 | 118s | 108,900 |

终态：画布 17 节点 / 16 依赖边；视频 1280×720 / 5088ms / 3.58MB，解码核验通过。

| 能力 | 状态 |
|---|---|
| 豆包文本（JSON 模式） | ✅ `doubao-seed-2-1-turbo-260628` |
| Seedream lite 文生图 / 图生图 | ✅ `doubao-seedream-5-0-260128` |
| Seedream pro 文生图 / 图生图 | ✅ `doubao-seedream-5-0-pro-260628` |
| Seedance mini 图生视频 | ✅ 任务 `cgt-20260920211952-twjct` |
| seedance-fast / 2.5 | ⏸ 未发起真实任务 |

**这次真实运行暴露并修掉的 4 个缺陷**（mock 永远测不出来，详见 real-verification.md §3）：
模型幻觉 Clip id 被拿去反查项目、模型返回中文枚举导致 400、
资产方案建边被自己的手动拖线规则拦下、真实文本调用远超脚本等待上限。
前三个已固化为回归测试。

**费用**：`usage_records` 记录真实用量，但单价未与方舟账单核对，
因此状态为 `unknown`、金额为 `null`（**不是 0**）——0 会被误读成免费。


---

## 11. 可靠性回归（《2026-09-20 审查.md》P1）— mock 下可重复

全部落在 `backend/tests/test_review_fixes.py`（18 条），按报告分节组织。
前提是 §0 的环境要求成立（强制测试库 + 会话锁），否则这些结论不成立。

| 场景 | 用例 | 断言要点 |
|---|---|---|
| 异常不收敛（任务永久 `running`） | `test_unhandled_exception_converges_to_failed_and_releases_lock` | 未捕获异常 → `failed` + `GENERATION_FAILED`，节点锁释放，同节点可重新发起 |
| 报告点名的触发点 | `test_illegal_clip_no_from_model_fails_as_provider_rejected` | 模型返回 `clipNo="第三场"` → `PROVIDER_REJECTED`（不再退化成 GENERATION_FAILED） |
| 租约回收 | `test_stale_lease_is_requeued_and_audited` | 过期 `running` → `pending`，`recoveryCount=1` 且写入 `recoveryReason` |
| 在途任务不被回收 | `test_submission_with_inflight_provider_task_is_not_requeued` | 有 `provider_task_id` 时不回队，交给 reconciler，避免重复计费 |
| 双 worker | `test_concurrent_workers_claim_one_pending_submission` | 两个线程并发 `run_pending`，同一个 `pending` 只被领取一次 |
| 重复提交（幂等） | `test_video_plan_retry_reuses_plan_and_leaves_no_orphans` | 同 key 重放复用 submission 与方案，画布节点数不变，`frozenReferenceCount` 如实返回 |
| 重复点击（节点锁） | `test_video_plan_second_click_is_blocked_by_clip_node_lock` | 不同 key 的第二次点击 409，且**不留下空方案或孤儿节点**；任务结束后锁释放 |
| 供应商超时恢复 | `test_provider_timeout_keeps_attempt_reconcilable_and_recovers` | 超时后提交保持 `running`、`providerTaskId` 保留；reconciler 复查后交付成功、落盘可解码、attempt 仍只有一条 |
| 供应商不可恢复 | `test_unrecoverable_provider_task_stops_retrying_without_resubmitting` | 有限次复查后 `PROVIDER_TASK_UNRESOLVED`，锁释放，**没有重建过供应商任务** |

业务语义回归（报告 §三）同文件：绑定 revision、Clip 候选复制、剧本分支过滤、
结果节点编辑/优化/抽卡、保存 B 与优化 attempt 分离、立绘派生源图。

---

## 未验收 / 已知边界

| 项 | 说明 |
|---|---|
| seedance-fast / 2.5 规格与计价 | 未实测，界面显式标注「未核验」，不计入 `realAvailable` |
| 各档位单价 | 未与方舟账单核对，费用显示「待核对」而非金额 |
| 并发上限、限流行为、长期凭证策略 | 未核验 |
| 多集管理、批量扫描、打包迁移、成片合成 | PRD 明确暂缓，未实现 |
| 手动创建文本节点 | 需要先有业务对象（导入或生成），未提供「空文本节点」接口 |

## 复现方式

```powershell
# 后端（默认 8010；本机被旧项目占用时用 8020）
$env:PYTHONPATH="backend\.pylibs;backend"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8020

# 跑一遍完整链路
$env:MANJU_BACKEND_PORT=8020
python backend\scripts\seed_demo.py --user admin --password admin123

# 前端
npm run dev --prefix frontend        # MANJU_BACKEND / MANJU_WEB_PORT 可覆盖

# 截图
python backend\scripts\shot.py --url /login --out docs\screenshots\01-login.png
```
