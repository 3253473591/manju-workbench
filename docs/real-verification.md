# real 模式核验记录

本文记录**真实火山方舟调用**的实测结果，是 PRD §9 第 10 条与 [API对接文档 §8](baseline/API对接文档.md) 验收清单的证据。
未列出的档位一律视为**未核验**，不允许在界面或接口上显示为可用。

核验方式：`backend/scripts/smoke_real.py`（不写密钥，只记录模型、耗时、usage、文件 hash 与交付结论）。

## 1. 核验结果总览

| 能力 | 档位 | 接入点 | 实际模型（供应商响应） | 核验时间 | 结论 |
|---|---|---|---|---|---|
| 文本 | doubao | `ep-20260917112408-5v5pr` | `doubao-seed-2-1-turbo-260628` | 2026-09-20 | ✅ 通过 |
| 图片 | seedream-lite | `ep-20260920164208-q8z6z` | `doubao-seedream-5-0-260128` | 2026-09-20 | ✅ 通过（文生图 + 单源图生图） |
| 图片 | seedream-pro | `ep-20260920164144-9g86q` | `doubao-seedream-5-0-pro-260628` | 2026-09-20 | ✅ 通过（文生图 + 单源图生图） |
| 视频 | seedance-mini | `ep-20260911123900-jzrcv` | 接入点已受理并产出可解码视频 | 2026-09-20 | ✅ 通过 |
| 视频 | seedance-fast | `ep-20260911124121-s98bf` | — | — | ⏸ 未发起真实任务 |
| 视频 | seedance-2.5 | `ep-20260911124159-mkx2n` | — | — | ⏸ 未发起真实任务 |

## 2. 逐项证据

### 2.1 豆包文本

- 请求：`POST /chat/completions`，`response_format = {"type":"json_object"}`
- 结果：HTTP 200，耗时 11.5s
- 实际模型：`doubao-seed-2-1-turbo-260628`
- 用量：`prompt_tokens = 60`、`completion_tokens = 447`
- 响应内容可直接 `json.loads`，字段为 `title` / `body`

> 与 API对接文档 §3.0 的历史记录一致：仅靠提示词约束 JSON 不可靠，必须显式请求 JSON 模式，
> 服务端仍需做 schema 校验（`generation_service.parse_json_payload`）。

### 2.2 Seedream 文生图

| 档位 | 耗时 | 实际模型 | usage | 产物 |
|---|---:|---|---|---|
| lite | 21.1s | `doubao-seedream-5-0-260128` | `generated_images=1, output_tokens=14400` | 499,389 bytes，JPEG |
| pro | 29.4s | `doubao-seedream-5-0-pro-260628` | `generated_images=1, output_tokens=14400` | 479,051 bytes，JPEG |

### 2.3 Seedream 单源图生图

| 档位 | 耗时 | 实际模型 | usage | 产物 |
|---|---:|---|---|---|
| lite | 22.8s | `doubao-seedream-5-0-260128` | `generated_images=1` | 181,111 bytes |
| pro | 98.1s | `doubao-seedream-5-0-pro-260628` | `input_images=1, generated_images=1` | 351,583 bytes |

请求尺寸 `1920x1920`。`1024x1024` 会被接口以「image size must be at least 3686400 pixels」拒绝，
该限制已写入模型配置的 `spec.minPixels`，并在 `ArkImageProvider` 提交前校验（避免无效计费）。

### 2.4 Seedance 图生视频

- 请求：`POST /contents/generations/tasks`，1 张参考图，`720p`、`16:9`、5 秒
- 供应商任务 ID：`cgt-20260920211952-twjct`（创建后立刻持久化，用于恢复查询）
- 耗时：93.7s（含轮询与下载）
- usage：`completion_tokens = 108900`
- 交付核验：mp4 3,614,945 bytes；`ffprobe` 读出 `1280x720`、`5088ms`
- 首帧缩略图：成功提取

> 交付核验用的是**落盘后的文件**，不是 HTTP 200 或 URL——供应商成功不等于交付成功。

## 3. 完整 DB 链路（一次连续操作，全部真实调用）

项目 `real 全链路验收：师尊别砍我系统`（`01M2ZHP6Z36RPS24D3Y8HQ21Y3`），
从空项目一路跑到可播放的视频结果，**14 个 attempt 全部是真实调用**：

| # | 步骤 | 耗时 | 输出用量 | 结果 |
|---|---|---:|---:|---|
| 1 | 人物设定 `setting_generate` | 82s | 4,297 tokens | ✅ |
| 2 | 生成剧本 `script_generate` | 155s | 7,352 tokens | ✅ |
| 3 | 分镜拆分 `script_split_clips` | 188s | 12,678 tokens | ✅ 拆出 5 个 Clip |
| 4 | 人物立绘规划 `plan_characters` | 71s | 3,456 tokens | ✅ 林晚 / 沈知夏 |
| 5 | 确认清单 | — | — | ✅ 2 个图片方案节点，回填 10 条引用 |
| 6 | 出图 `image_draw` ×2 | 20s / 22s | 14,400 each | ✅ 1920×1920，解码核验通过 |
| 7 | 立绘联合派生 `image_derive` | 59s / 67s | 14,400 ×2 | ✅ 面部特写 + 三视图 |
| 8 | 视频方案 `video_plan_create` | 57s | 2,772 tokens | ✅ 冻结 2 张参考图 |
| 9 | 视频抽卡 `video_draw` | 118s | 108,900 tokens | ✅ 1280×720 / 5088ms / 3.58MB，解码核验通过 |

终态：画布 17 个节点、16 条依赖边；`usableResultCount` 对每个资产都收敛到 1，
视频方案前置校验才放行。

### 这次真实运行暴露并修掉的问题（mock 永远测不出来）

1. **模型给的 `clipIds` 是幻觉**（实测返回 `clip_001`…`clip_005`）。
   原实现拿它去反查项目，查不到就退回到「库里的第一个项目」——
   会把资产挂到别的项目上。已改为以**提交本身**为权威范围（submission → episode → 该集全部 Clip），
   幻觉 id 一律丢弃并在响应里回报 `droppedClipIds`。
2. **模型用中文枚举**（实测 `assetType="人物"`、`imageKind="角色立绘"`、`variantName="默认造型"`）。
   原实现直接 `AssetType(value)`，直接 400。已加词表归一，
   认不出的值带着原值报错而不是静默兜底。
3. **资产方案节点建边走了手动拖线规则**，`text → image` 被自己拦下。已按 PRD §7 走系统豁免。
4. **真实文本调用极慢**（单次 71–188 秒），原脚本 180 秒等待不够。
   已新增 `scripts/continue_real_demo.py` 支持断点续跑。

以上 1、2、3 都已固化为回归测试（`tests/test_acceptance_extra.py`）。

## 4. 费用

按用户 2026-09-20 提供的当前官方报价折算（计价逻辑见 `app/services/pricing.py`，
单价存在 `model_configs.spec_json.pricing`，改价不用改代码）。

全链路 14 个 attempt 合计 **4.405929 元**：

| 操作 | attempt | 金额（元） |
|---|---:|---:|
| setting_generate | 1 | 0.064947 |
| script_generate | 1 | 0.113328 |
| script_split_clips | 1 | 0.197229 |
| plan_characters | 3 | 0.163455 |
| image_draw（lite） | 2 | 0.440000 |
| image_derive（lite） | 4 | 0.880000 |
| video_plan_create | 1 | 0.042270 |
| video_draw（mini 720p 5s） | 1 | 2.504700 |

计价口径要点：

- **豆包输入与输出单价不同**（3 / 15 元每百万），必须分开乘——早期实现拿一个单价乘两边，算错了。
- **视频按输出 token 计价**，且分「含 / 不含视频输入」两档。本版只发参考图，按**不含视频输入**计价。
- **seedance 2.5 还要按分辨率分档**（480P/720P 70、1080P 77 元每百万）。
- **seedream pro 按输出图像素分档**（≤261 万 0.3、>261 万 0.6 元/张），输入图 0.02 元/张且首张免费。
  注意：官方要求最小面积 3,686,400 像素，**已经高于 261 万像素档**，
  因此本版常用尺寸都会落在 0.6 元/张那一档。
- 金额统一记 `billing_status = estimated`（按回执用量折算，未经账单确认）。
  **单价缺失或口径不认识时记 `unknown` 与 `null`，绝不记 0**——0 会被读成「免费」。

### ⚠️ 与手册记载的单价差 2.5 倍，建议核对

手册 §3.1 记的是 mini 720P **0.200 元/秒**（个人户，2026-09-18 调价后），
即 5 秒 ≈ 1.00 元；本次按你给的单价折算，5 秒 mini 720P = **2.5047 元**，约 0.50 元/秒。

| 口径 | mini 720P 5s | 换算元/秒 | 手册 24 clip ≈ |
|---|---:|---:|---:|
| 手册记载（0.200 元/秒） | 1.00 | 0.200 | 56 元 |
| 本次给的单价（23 元/百万 tokens） | 2.5047 | 0.499 | 约 140 元 |

差异可能来自：官方再次调价、账号档位不同（手册区分个人户/企业户）、
或「tokens/秒」这个换算率已经变化（本次沿用手册的 21,690 tokens/秒 @720p）。
**建议在方舟控制台核对一次真实账单**；核对后只要改 `spec_json.pricing`，
用 `scripts/recalc_usage.py` 就能把历史账重算，不需要重跑生成。

### 单价改动怎么处理

```powershell
# 改 model_configs 的计价口径后（或直接改 seed.py 的 PRICING_* 常量并重启后端）
python backend\scripts\recalc_usage.py --dry-run   # 先看差异
python backend\scripts\recalc_usage.py             # 提交
```

## 5. 密钥处理

- 6 把 Key 只写入 `backend/.env`，该文件已在 `.gitignore` 中排除，绝不提交。
- 代码、文档、日志、API 响应、输入快照里都只有环境变量**名**（`credential_ref`），没有值。
- `user` 角色的任何响应都不含计费字段；`credential_ref` 的实际值不出现在任何响应里。
- `backend/.env.example` 只含占位符，可直接提交。

> ⚠️ 已有凭证通过聊天传递，建议到方舟控制台**轮换这批 Key**；轮换后只需改 `.env`。

## 6. 尚未核验

- `seedance-fast` 与 `seedance-2.5` 的规格（分辨率、时长、参考图上限）与计价：未发起真实任务。
- 各档位的并发上限与限流行为。
- 各档位的**单价**：需要在方舟控制台核对后填入模型配置，费用才会从「待核对」变成实际金额。

## 7. 复现

```powershell
$env:APP_MODE="real"        # 或直接改 backend\.env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8020

$env:MANJU_BACKEND_PORT=8020
python backend\scripts\seed_demo.py --name "real 全链路验收"
python backend\scripts\continue_real_demo.py <projectId>   # 文本步骤较慢时续跑
```

