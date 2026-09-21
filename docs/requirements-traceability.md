# 需求追溯矩阵

用途：把「PRD 条款 → 外部手册条款 → 代码入口 → 测试 → 证据」串成一条可核对的链。
来源：《2026-09-20 审查.md》§6（与《AI漫剧制作工作流执行手册》的 14 个对齐维度）与
§二/§三（10 条 P1/P2 代码问题）。

**完成判据**（审查报告 §8）：每个维度都必须同时给出「决策 + 代码入口 + 测试证据」三项，
缺任一项即视为未完成。下表中任何标 `⚠` 的条目都不得计为已对齐。

> **当前状态（不要读成「已全部完成」）**
>
> - **14 个对齐维度：11 个有「决策 + 代码入口 + 测试证据」，3 个仍是未完成项**（`⚠`）：
>   H4 图片多图 provider 协议与上限核验、H6 价格/并发核验、G7 real 单 Clip 复跑。
> - **审查报告 §二/§三的 10 条 P1/P2 代码问题**：代码已改并各有用例（P1-1…P2-10 全表可查），
>   但**验证只到 mock 层**；real 能力、价格与并发口径仍未核验。
> - 因此**整体不能标记为「与执行手册已对齐」**，也不能标记为「审查报告逐项完成」。
>   只有 14 个维度全部三项齐全且 P0/P1 通过，才允许那样写。
>
> **本轮验证的实际条件**（换环境结论可能不成立，见
> [`acceptance.md` §0](acceptance.md)）：MySQL 8、测试库名以 `_test` 结尾、
> 由 `conftest.py` 强制指定、单会话独占（并发会话会被 `GET_LOCK` 串行化）。
> 测试总数 **117**；`base → head` 迁移往返通过；`alembic check` 在迁移到 head 的库上
> 报 `No new upgrade operations detected`；前端 `npm run build` 通过。
> 前端构建与 OpenAPI 校验只证明「可构建 / 契约一致」，**不证明后端业务闭环正确**。

文档权威口径：

- 业务需求编辑源：仓库根目录 `PRD.md`。
- `manju-director/docs/baseline/PRD.md`：同步副本，只为仓库独立分发，不单独编辑。
  一致性检查：`git diff --no-index PRD.md manju-director/docs/baseline/PRD.md`
  （只允许第 1 节文档说明与相对链接差异）。
- `AI漫剧/AI漫剧制作工作流执行手册.md`：外部制作流水线 / 经验手册，
  **不是**本 Demo 的需求、接口或验收规范（已在手册顶部加定位声明）。

---

## 一、与执行手册的 14 个对齐维度（审查报告 §6）

### 1.1 硬冲突（H1–H7）

| # | 维度 | 决策（口径裁定） | 代码入口 | 测试 / 证据 |
|---|---|---|---|---|
| H1 | 产品载体 | 以 PRD 的对象模型为准：项目/单集/画布节点/抽屉/版本/媒体结果构成可刷新恢复的闭环。手册的文件夹产物**不作为**交付状态，只作为导入来源。 | `app/models/{project,canvas,content,media,generation}.py`、`app/api/routers/canvas.py`、`frontend/src/pages/WorkspacePage.tsx` | `tests/test_mock_chain.py::test_full_mock_chain`（第 12 步刷新后节点、布局、边、媒体仍可读） |
| H2 | 阶段顺序 | 采用 PRD 顺序：大纲/设定 → 剧本 → Clip/分镜 → 资产规划 → 图片方案/结果 → 视频方案/结果。手册阶段只能作为外部执行器流程。 | `app/services/text_ops.py`（`HANDLERS`）、`app/services/media_ops.py`（`RUNNERS`）、`app/services/canvas_service.py::STAGE_RANK` | `tests/test_mock_chain.py::test_full_mock_chain`（全链路按顺序走通） |
| H3 | 模型与供应商 | Demo 只接火山方舟官方：豆包（文本）、Seedream lite/pro（图片）、Seedance mini/fast/2.5（视频）。手册的 k/q/m/liaobots **不进入**产品适配器。 | `app/providers/{ark,mock,registry}.py`、`app/core/config.py`（`ARK_*`） | `tests/test_real_mode_guard.py`、`tests/test_openapi_conformance.py` |
| H4 | 图片生成范围 | 取消「图片图生图永久单源」限制；改用与视频共用的**结构化参考图片列表**（槽位、用途、顺序、文件 key、sha256、媒体版本），单图是列表长度为 1 的特例。 | `app/services/media_service.py::validate_video_references` / `freeze_video_references`、`app/schemas/media.py` | `tests/test_mock_chain.py::test_video_plan_requires_exactly_one_usable_result`、`tests/test_acceptance_extra.py::test_new_video_plan_does_not_rewrite_old_frozen_context` ⚠ 图片多图 provider 协议与上限核验未完成（无已核验多图图片模型） |
| H5 | 视频链路 | 保留 PRD 语义：Clip 生成视频方案 → 冻结参考图与槽位 → 用户选版后抽卡 → 每个结果独立落盘。手册的 m 草稿 / 中转只算外部执行策略。 | `app/api/routers/clips.py::create_video_plan`、`app/services/media_ops.py::{handle_video_plan_create,handle_video_draw}` | `tests/test_acceptance_extra.py::test_new_video_plan_does_not_rewrite_old_frozen_context`、`tests/test_review_fixes.py::test_provider_timeout_keeps_attempt_reconcilable_and_recovers` |
| H6 | 能力/价格状态 | 只展示已核验能力：`ModelConfig.verified_at/actual_model` 由真实调用写入，`/meta` 的 `realAvailable` 只看该字段；未核验值只能作历史参考。 | `app/services/generation_service.py::mark_model_verified`、`app/services/pricing.py`、`app/api/routers/system.py` | `tests/test_pricing.py`、`tests/test_real_mode_guard.py` ⚠ 价格/分辨率/并发仍以手册数字作 seed，需接入核验记录后才可标为已对齐 |
| H7 | 交付与确认 | 服务端保存 logical/physical/delivery 三层状态；媒体必须落盘、可定位、可解码才算 delivered。人工确认只写审美/业务决定。 | `app/models/generation.py`、`app/services/generation_service.py::{record_delivery,mark_delivery_verified}` | `tests/test_mock_chain.py::test_full_mock_chain`（视频结果 decodeStatus=verified、首帧可取、文件可读） |

### 1.2 PRD 要求但手册未覆盖的缺口（G1–G7）

| # | 缺口 | 决策 | 代码入口 | 测试 / 证据 |
|---|---|---|---|---|
| G1 | 草稿/不可变历史/输入快照/分支继承/绑定 revision 的精确定义 | 草稿（`document.body`）与不可变 `content_revisions` 分离；剧本绑定 `bound_setting_revision_id` 与 `bound_outline_revision_id`，拆分与规划一律读绑定版本，缺失即报错，禁止静默回退。 | `app/models/content.py`、`app/services/content_service.py`、`app/services/text_ops.py::{_bound_setting_document,handle_script_split_clips}` | `tests/test_content_versions.py`、`tests/test_review_fixes.py::{test_split_clips_uses_bound_setting_revision_not_latest,test_split_clips_fails_loudly_when_bound_revision_is_gone}` |
| G2 | 服务端幂等、节点锁、任务租约、异常收敛、重启恢复、供应商 reconciler | 幂等键先于建图判定；`lock_key` 为稳定节点锁；`claimed_at/claimed_by/heartbeat_at` 租约 + 原子 claim；未捕获异常一律收敛；在途供应商任务由 reconciler 按 `provider_task_id` 复查，绝不重新提交。 | `app/services/generation_service.py`、`app/services/provider_reconciler.py`、`app/services/worker.py`、`backend/alembic/versions/0002_generation_leases.py` | `tests/test_review_fixes.py` 的 P1 段共 9 条（兜底收敛、租约回收、在途任务不回收、原子领取、幂等重放、节点锁、reconciler 恢复、不可恢复收敛、非法 clipNo） |
| G3 | 结果节点继续编辑 B、优化、抽卡，以及源图/seed/提示词版本链 | 结果节点的提示词归属其**迭代方案**（`params.iterationPlanOf`，每个结果只对应一个，重复点击复用）；保存 B 只追加 prompt version；优化落独立 text attempt；抽卡从结果节点可直接发起并保留来源结果/seed/参考图快照。 | `app/api/routers/media.py::{_followup_plan,create_prompt_version,optimize_prompt,draw_media}`、`app/services/media_service.py::prompt_owner_node`、`frontend/src/components/NodeDrawer.tsx` | `tests/test_review_fixes.py::{test_result_node_draw_creates_reusable_iteration_plan,test_prompt_optimize_on_result_node_builds_iteration_plan,test_result_node_visible_prompt_can_be_edited,test_prompt_optimize_records_attempt_while_saving_b_does_not}` |
| G4 | 三角色权限、计费字段隔离、预估/单价/attempt 费用、mock 不计实际支出 | `user` 响应不含任何计费字段；`manager/admin` 可读费用；`usage` 缺失一律记 unknown/None，不默认 0；mock 记 `billingStatus=mock`。 | `app/api/deps.py`（`BillingViewer`/`EditorUser`/`AdminUser`）、`app/services/billing_service.py`、`app/services/pricing.py` | `tests/test_permissions.py`、`tests/test_pricing.py`、`tests/test_mock_chain.py`（断言 user 的 `attempt.usage is None`） |
| G5 | 单文件导入、完全匹配命名、参考图引用回填、屏蔽不级联、精确候选校验 | 命名解析集中在 `naming.py`；确认清单回填真实 Clip 引用并丢弃幻觉 clipId；屏蔽只影响当前节点不级联；参考候选必须「唯一未屏蔽且文件可用」。 | `app/services/{naming,asset_plan,asset_service}.py`、`app/api/routers/{files,assets}.py` | `tests/test_acceptance_extra.py::{test_confirm_plan_normalizes_model_vocabulary_and_drops_fake_clip_ids,test_asset_type_alias_helper}`、`tests/test_mock_chain.py::test_video_plan_requires_exactly_one_usable_result` |
| G6 | 隐藏 A 不得通过 API、来源查询或媒体正文返回；B 编辑与提示词优化 attempt 分离 | 隐藏预设与 `final_input_text` 只存服务端、不进任何响应模型；B 编辑只追加 prompt version（无 attempt），优化必须创建 text attempt 并写输入快照。 | `app/core/prompts.py`、`app/schemas/media.py`、`app/services/media_service.py::get_media_node_detail`、`app/api/routers/media.py::optimize_prompt` | `tests/test_mock_chain.py`（断言响应无 `hidden`/`finalInputText`）、`tests/test_review_fixes.py::test_prompt_optimize_records_attempt_while_saving_b_does_not` |
| G7 | PRD 的 10 条 Demo 验收（mock 全链路、real 单 Clip、刷新恢复、三角色可见性） | mock 全链路与刷新恢复已可重复验收；real 单 Clip 需要已核验凭证，缺凭证时记为「real 未验收」，不以 mock 代替。 | `backend/tests/`、`docs/acceptance.md`、`docs/real-verification.md` | `tests/test_mock_chain.py`、`tests/test_permissions.py`、`tests/test_review_fixes.py` ⚠ real 单 Clip 需在有凭证的环境执行并回填 `docs/real-verification.md` |

---

## 二、P1/P2 代码问题（审查报告 §二、§三）

| # | 问题 | 决策 | 代码入口 | 测试 / 证据 |
|---|---|---|---|---|
| P1-1 | 生成任务可能永久停留在 `running` | 三层收敛：已知异常/未捕获异常一律收敛到终态并释放锁；`running` 带心跳与租约，过期回收为 `pending`；**有在途供应商任务时不回收**，交给 reconciler；重试次数与原因写入审计。 | `app/services/generation_service.py::{run_submission,converge_failure,recover_stale_submissions,has_inflight_provider_task}`、`app/services/worker.py`、`app/models/generation.py` | `tests/test_review_fixes.py::{test_unhandled_exception_converges_to_failed_and_releases_lock,test_illegal_clip_no_from_model_fails_as_provider_rejected,test_stale_lease_is_requeued_and_audited,test_submission_with_inflight_provider_task_is_not_requeued}` |
| P1-2 | 视频方案创建顺序破坏幂等并产生孤儿节点 | 幂等键最先判定并复用原提交/原方案；稳定 `lock_key` 在建图**之前**检查；方案 + 冻结快照 + 画布节点 + 依赖边 + submission 同一事务，失败整体回滚。 | `app/api/routers/clips.py::create_video_plan`、`app/services/generation_service.py::create_submission`、`app/services/canvas_service.py::create_edge(commit=False)` | `tests/test_review_fixes.py::{test_video_plan_retry_reuses_plan_and_leaves_no_orphans,test_video_plan_second_click_is_blocked_by_clip_node_lock}` |
| P1-3 | 多 worker 领取同一个 pending 任务 | 领取用 `UPDATE ... WHERE logical_status = pending` 的原子条件更新，行数不为 1 即放弃；不再依赖「先查后改」。 | `app/services/generation_service.py::run_pending` | `tests/test_review_fixes.py::test_concurrent_workers_claim_one_pending_submission` |
| P1-4 | 供应商视频任务超时后没有恢复轮询 | `VideoProvider.resume(task_id)` 只查询既有任务；worker 每轮复查 `accepted/running/unknown` 的 attempt，成功则走与抽卡相同的交付路径；未知状态有限次重试后收敛为 `PROVIDER_TASK_UNRESOLVED` 并释放锁，**绝不自动重新提交**。 | `app/providers/base.py::VideoProvider.resume`、`app/providers/ark.py::ArkVideoProvider.resume`、`app/services/provider_reconciler.py`、`app/services/media_ops.py::deliver_video_result` | `tests/test_review_fixes.py::{test_provider_timeout_keeps_attempt_reconcilable_and_recovers,test_unrecoverable_provider_task_stops_retrying_without_resubmitting}` |
| P2-5 | 剧本拆分使用最新人物设定，违反绑定版本 | 始终按 `script.bound_setting_revision_id` 读取该 revision 的正文；绑定版本不存在时明确报错。 | `app/services/text_ops.py::{_bound_setting_document,handle_script_split_clips}` | `tests/test_review_fixes.py::{test_split_clips_uses_bound_setting_revision_not_latest,test_split_clips_fails_loudly_when_bound_revision_is_gone}` |
| P2-6 | Clip 优化候选丢失参考资产和内部镜头 | 候选 Clip 在同一事务里复制 `AssetReference`（含槽位、必填、来源）与 `ClipShot`（含时间与文本字段），并继承 `document_id` 分支归属。 | `app/services/text_ops.py::handle_clip_optimize` | `tests/test_review_fixes.py::test_clip_optimize_candidate_keeps_references_and_shots` |
| P2-7 | 人物立绘规划会串入其他剧本分支 | Clip 按 `document_id == 当前剧本`（历史行回退到该剧本的 revision 集合）过滤；人物设定按剧本绑定的 setting 文档过滤；输入快照记录脚本、revision、Clip 与人物。 | `app/services/text_ops.py::{_script_branch_clips,_bound_setting_document,_episode_character_names,_plan_assets}` | `tests/test_review_fixes.py::test_character_planning_stays_inside_script_branch` |
| P2-8 | 媒体结果节点无法继续编辑、优化和抽卡 | 结果节点通过迭代方案继续生成；后端 `draw` / `prompt-optimize` / `prompt-versions` 都接受结果节点；前端解除结果节点的提示词编辑禁用并给出优化入口。 | `app/api/routers/media.py::{_followup_plan,draw_media,optimize_prompt,create_prompt_version}`、`frontend/src/components/NodeDrawer.tsx` | `tests/test_review_fixes.py` 的结果节点 4 条用例；前端 `npm run build` 通过 |
| P2-9 | 修改 B 后没有独立提示词优化 attempt | 拆为两个操作：`POST /media-nodes/{id}/prompt-versions` 只追加 prompt version（无模型调用）；`POST /media-nodes/{id}/prompt-optimize` 必创建 text attempt（含输入快照与费用）。 | `app/api/routers/media.py::{create_prompt_version,optimize_prompt}`、`docs/openapi.yaml` | `tests/test_review_fixes.py::test_prompt_optimize_records_attempt_while_saving_b_does_not` |
| P2-10 | 立绘派生可能使用错误的源图 | 派生以**当前选中结果**的 `media_item_id` 为源图，`source_media_id` 只作为溯源信息写入 attempt 快照（`provenanceMediaId`），不再覆盖用户选择。 | `app/services/media_ops.py::handle_image_derive` | `tests/test_review_fixes.py::test_derive_uses_currently_selected_result_image` |

---

## 三、尚未完成的事项（不得标记为已对齐）

| 项 | 现状 | 缺什么 |
|---|---|---|
| H4 图片多图 provider 适配 | 共享参考图管理/校验/冻结已经在视频链路上落地；图片侧仍只发单张源图 | 需要已核验的图片模型多图接口能力（顺序、用途字段、数量与尺寸上限、seed），并据此实现图片侧多图请求与调用前拦截 |
| H6 能力/价格核验 | 已有 `verified_at`/`actual_model` 与未核验即不可用的机制 | 需要把手册中的日期化分辨率、时长上限、单价、并发逐项接入核验记录 |
| G7 real 单 Clip 验收 | mock 全链路可重复验收 | 需要有效凭证在实际环境跑一次并回填 `docs/real-verification.md` |
| 参考图列表的前端展示组件 | 抽屉已能展示冻结参考图（顺序不可变） | 尚未抽取为图片/视频共用的「参考图列表」组件，图片方案侧也没有槽位/用途编辑入口 |
| 执行手册 P2 降级 | 手册顶部已加定位声明 | 手册内部逐条的「适用 provider + 验证日期」标注尚未完成，日期化价格/并发仍散落正文 |

---

## 四、怎么跑这些证据

**先满足前置条件**（否则结论不可复现，见 [`acceptance.md` §0](acceptance.md)）：
测试库由 `conftest.py` 强制指定为以 `_test` 结尾的库；不要用 `DATABASE_URL`
（那是给应用跑的）；同一测试库同时只应有**一个** pytest 会话（并发会被 `GET_LOCK` 串行化）。
环境本身先用 `python backend\scripts\check_env.py` 过一遍，或直接跑
`.\scripts\verify_clean_env.ps1`（锁定依赖 + 核对用例数）。

```bash
# 后端全量 117 项（含本矩阵引用的所有回归用例）
cd manju-director/backend
run.cmd -m pytest              # Windows；等价于 python -m pytest（不要再加 -q，会压掉汇总行）

# 契约一致性（新增/变更接口必须同步 docs/openapi.yaml）
run.cmd scripts/export_openapi.py
run.cmd scripts/validate_openapi.py

# 迁移往返与漂移（对已迁到 head 的库跑）
run.cmd -m alembic -c alembic.ini downgrade base
run.cmd -m alembic -c alembic.ini upgrade head
run.cmd -m alembic -c alembic.ini check

# 前端构建（只证明可构建，不证明后端闭环）
cd manju-director/frontend && npm run build

# 两份 PRD 的一致性
git diff --no-index PRD.md manju-director/docs/baseline/PRD.md
```

真实结果请写进 [`acceptance.md`](acceptance.md) 的表格（环境 + 命令 + 结论），
不要在这里复述结论。
