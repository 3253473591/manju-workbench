纯 HTML/CSS，不接 API、不发请求、零外链，双击即可打开。

- `prototype/login.html` — 登录页（入口，`index.html` 会跳到这里）
- `prototype/workspace.html` — 工作区，**主视图是分镜表**
- 登录页预填演示账号，口令 ≥ 10 位即可进入；右上角可切换 `lead / user / admin` 演示身份

评审辅助参数：

| 参数 | 作用 |
|---|---|
| `?open=1…8` | 直接展开某个片段的分镜详情 |
| `?view=cost` / `?view=wall` | 成本台账 / 镜头墙（待接入说明页） |
| `?modal=1` | 付费点确认弹窗（预览，按钮不发起请求） |
| `?nav=collapsed` | 侧栏收起态 |
| `?grid=1` | 对齐栅格 + 12 列导轨 |

快捷键：`⌘/Ctrl + B` 折叠侧栏，`G` 开关栅格，`Esc` 关闭弹窗。

## 主视图为什么是分镜表

1. **只有它的字段是齐的。** `GET /episodes/{eid}/clips` + `GET /clips/{cid}` 能喂满分镜表要的全部结构：`duration_s / shots_json / dialogue_json / acceptance_json / ref_slots_json / status / source_idx`。
2. **它是唯一能验证规则的屏。** 时序闭合、台词内嵌、槽位连续这三条硬校验，在表里一眼可数；镜头墙验证不了任何一条。
3. **镜头墙已实现。** 缩略图与标题都已就绪（缩略图先用本地 480×270 占位）；仍缺**每片段成本**（`cost` 只按 model/day 聚合），所以卡片右下角显示版本数而不是金额。

流水线不是第三个页面：它退成左侧 5 个阶段入口 + 当前阶段高亮，`/steps/*` 未接入时点击给明确提示。

## 字段边界（页面里也标了同一条）

| 屏上元素 | 来源 | 状态 |
|---|---|---|
| 片段序号 / 源序（`源08`） | `clip.idx` / `clip.source_idx` | ✅ 已验证：插入镜播放序 ≠ 源片段号 |
| 时长 / 镜头数 / 台词数 | `duration_s` / `shots_json` / `dialogue_json` | ✅ |
| 槽位 `0/4` | `ref_slots_json[].asset_id` | ✅ 计数；**全部为 null**，资产未回填，属真实状态不是缺字段 |
| 验收 `5/5` | `acceptance_json` + `revision.acceptance_result_json` | ✅ 已按 `superseded_by` 语义取未被取代的判据 |
| 状态 chip | `clip.status` 六态 | ✅ 演示值 |
| 版本 `2 版` | `GET /clips/{cid}/revisions` 条数 | ✅（单个 revision 序号需用 `revision.id`，表里没有 `version` 列） |
| 计费 token / 金额 / 耗时 | `revision.input_tokens / cost / latency_ms` | ✅ 取 seed 真实值 |
| 动线 `samples / freeze / avg / max` | `revision.motion_json` | ✅ 按字段原名展示，不再写成"卡顿 0.11s" |
| 片段标题 | `clips` 列表返回 `title` | ✅ 已可用；注意 `01` §4.4 表里还没这一列，契约要同步 |
| 缩略图 | `GET /assets/{id}.thumb_url`（clips 列表已带 `output_asset_id`） | ⏳ 接口已就绪，前端待接入 |
| 槽位图 | `ref_slots[].asset_id` | ⚠️ 仍全为 null，未回填前只能显示 `asset_ref` |
| 生成 / Job / Trace / SSE / 预算水位 | `/clips/{cid}/generate`、`/jobs*`、`/events` 全 ⏳ | ❌ 已从主视图移出，仅留付费点弹窗做结构预览 |
| 成本按 stage 构成 | `group_by` 只支持 `model`/`day` | ❌ 已改成按 model，day 因 fixture 无时间戳置灰 |

## 这个改动删掉了什么

上下文 12k 分节条、按 stage 的成本构成、预算水位、SSE「实时」徽标、5 个 stage 完成 tick、幂等键展示、每片段金额、`7 计划` 合计、`SOURCE L19`、规则级 Skill 注入 chips、所有缩略图。理由都是同一个：**当前出参拿不到，画出来就是假数据**。

## 视觉系统

| 项 | 取值 |
|---|---|
| 黑 | `#000` 底 / `#0a0a0c` 面板 / `#0e0e11` 悬浮 / `#141419` 输入 |
| 灰阶文字 | `#f2f2f4 → #a9a9b2 → #6d6d78 → #45454f` |
| 细线 | `#17171c` / `#22222a` / `#30303a` |
| 语义色 | 绿 `#34d399` 受控·通过；琥珀 `#fbbf24` 钱·待补；玫红 `#fb7185` 失败；紫 `#a78bfa` Skill |
| 字号 | 正文 13 / 说明 11.5 / 元信息 10.5 / 微标 9.5；标题 16–19 |
| 数字 | 全部 `tabular-nums`，金额 6 位小数（与 `00` §3.3 一致） |
| 栅格 | 24px 微线（alpha .022）+ 120px 主轴（.05）；`G` 提亮成青绿导轨 + 12 列参考 |

## 数据来源

片段标题、时长、镜头、台词、验收、`sk1_*` 槽位名全部来自 `demo/seed/clips.json`；金额、token、耗时、`motion_json` 来自 `demo/seed/revisions.json`（17 次调用 / ¥40.00 / 4,572,900 tok / 211s）。合计 8 片段 94.00s、36 个槽位、13 条验收点——都由字段算出来，不写死。注：`project.json` 里的 `planned_total_seconds=68` 是**剧本计划值**，与实际 `sum(duration_s)=94` 不同，页面只显示后者。

## 文件

```
prototype/
  index.html        跳转入口
  login.html        登录页
  workspace.html    工作区（分镜表 / 成本台账 / 镜头墙占位 / 付费点预览）
  base.css          token、栅格、登录页
  app.css           工作区框架与组件（分镜表局部样式在 workspace.html 内）
  assets/*.jpg      13 张真实设定图缩略图（thumb-01…08 用于分镜表与镜头墙，slot-a…d 备用）
  screens/*.png     预览截图
```

## 明确没做

不接接口、不做真实视频流、不做双主题、不做移动端完整适配（1180 以下详情改单列，900 以下表格收紧，保证不横向滚动）。
