# 漫剧工作台 API 对接文档

更新日期：2026-09-20。

本文集中维护 **Demo 后端对接外部模型 API** 的配置、请求、响应、执行与验收口径；不是前端调用本系统的 REST API 清单。业务范围以 [PRD.md](PRD.md) 为准。

> 说明：本文是作者工作区文档的快照，正文中出现的 `AI漫剧/_krea2_pipeline/*.py` 等路径属于
> 作者的本地产线工具，**不随本仓库分发**；保留原文是为了让规格来源可追溯。

当前状态：已完成 Seedream lite/pro 各 1 次真实单源图生图实测；请求使用临时凭证，凭证值未写入文件。请求示例和规格以本次响应为准，仍不代表所有参数已完整核验。

## 1. 资料来源与适用范围

| 来源 | 本次提取内容 |
|---|---|
| [doubao_ask.py](AI漫剧/_krea2_pipeline/doubao_ask.py) | BASE、MODEL、messages 构造、文本响应与 usage 解析 |
| [s_ark.py](AI漫剧/_krea2_pipeline/s_ark.py) | TIERS、build_content、创建任务、查询任务、下载、usage |
| [README.md](AI漫剧/_krea2_pipeline/README.md) | 历史规格与价格说明，仅作追溯 |
| 用户于 2026-09-20 提供 | Doubao-Seedream-5.0-pro / lite 的模型名称与接入点映射 |

- 只接火山方舟官方：豆包负责文本及提示词，Seedream 负责图片，Seedance 负责视频。
- `s_submit.py`、`poll_video.py` 是第三方中转实现，不是本 Demo 的官方接入模板。
- 旧执行器的自动追加测试图片、第三方回退、硬编码输入目录及全量并发提交，不迁入 Demo。错误信息中未点名某张图，不等于该图已经通过全部校验。
- [火山方舟官方文档入口](https://www.volcengine.com/docs/82379)用于接入时复核。此次浏览工具不可用，普通 HTTP 获取的页面也未包含可核验的 API 正文，故没有把在线规格或价格标记为已核实。

## 2. 通用连接与配置

```text
Base URL: https://ark.cn-beijing.volces.com/api/v3
Authorization: Bearer <对应模型配置的 API Key>
Content-Type: application/json
```

### 2.1 已收集的模型与接入点

下列值来自源码、用户提供及真实请求响应，来源逐项标注。接入点 ID 不等于公开模型 ID；“实际模型”是供应商响应中的 `model`，两者都要保存。

| 用途/产品标签 | 接入点 ID | 实际模型/产品名 | 来源及核验状态 |
|---|---|---|---|
| 豆包文本/提示词 | `ep-20260917112408-5v5pr` | `doubao-seed-2-1-turbo-260628` | 2026-09-20 HTTP 200 实测；`chat/completions` 与 usage 正常 |
| Seedance mini | `ep-20260911123900-jzrcv` | 待实测 | `s_ark.py` 的 `TIERS.mini.model`；尚未发起真实任务 |
| Seedance fast | `ep-20260911124121-s98bf` | 待实测 | `s_ark.py` 的 `TIERS.fast.model`；尚未发起真实任务 |
| Seedance 2.5 | `ep-20260911124159-mkx2n` | 待实测 | `s_ark.py` 的 `TIERS["2.5"].model`；尚未发起真实任务 |
| Seedream lite（默认） | `ep-20260920164208-q8z6z` | `doubao-seedream-5-0-260128` | 2026-09-20 单源图生图 HTTP 200 实测 |
| Seedream pro | `ep-20260920164144-9g86q` | `doubao-seedream-5-0-pro-260628` | 2026-09-20 单源图生图 HTTP 200 实测 |

旧代码为视频各档分别配置凭证，不能推断它们能共用一把 Key。新后端按模型配置解析凭证，模型配置持久化保存凭证引用，本文不保存密钥值。

### 2.2 建议的服务端环境变量

以下是 **新 Demo 的配置约定**，旧脚本尚未改为读取这些变量。

```dotenv
APP_MODE=mock
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3

ARK_DOUBAO_API_KEY=
ARK_DOUBAO_MODEL=ep-20260917112408-5v5pr

ARK_SEEDANCE_MINI_API_KEY=
ARK_SEEDANCE_MINI_MODEL=ep-20260911123900-jzrcv
ARK_SEEDANCE_FAST_API_KEY=
ARK_SEEDANCE_FAST_MODEL=ep-20260911124121-s98bf
ARK_SEEDANCE_25_API_KEY=
ARK_SEEDANCE_25_MODEL=ep-20260911124159-mkx2n

ARK_SEEDREAM_API_KEY=
ARK_SEEDREAM_LITE_MODEL=ep-20260920164208-q8z6z
ARK_SEEDREAM_PRO_MODEL=ep-20260920164144-9g86q
```

模型配置还应保存：实际模型标识、支持的输入方式/规格、参考图上限、seed 能力、价格及计价单位、并发上限、启停状态、核验时间。能力未知的 real 选项不能展示为已可用。

Key 仅后端读取，不进入浏览器、API 响应、日志、版本正文或输入快照。快照保存模型配置 ID/版本及非敏感规格。

## 3. 豆包：文本与提示词

### 3.0 已完成的真实冒烟测试

2026-09-20 使用用户提供的临时 Key 和 `ep-20260917112408-5v5pr` 完成文本请求。接口返回 HTTP 200，实际模型为 `doubao-seed-2-1-turbo-260628`，usage 可读取：普通请求一次为输入 77、输出 5014 tokens；启用结构化输出的一次为输入 56、输出 7263 tokens。

首次只用提示词约束“只输出 JSON”时，模型仍返回了 Markdown 代码块；加入 `response_format: {"type":"json_object"}` 后，响应内容可直接 `json.loads`，并得到 `title`、`scene`、`action`、`dialogue` 四个字段。因此 Demo 的结构化任务必须优先请求 JSON 模式，服务端仍需做 schema 校验，失败时保留原始响应并创建失败 attempt，不能直接写入业务正文。

### 3.1 请求

```http
POST /api/v3/chat/completions
```

从本地执行器提炼的非流式请求：

```json
{
  "model": "<ARK_DOUBAO_MODEL>",
  "messages": [
    {
      "role": "system",
      "content": [{"type": "text", "text": "<服务端任务模板>"}]
    },
    {
      "role": "user",
      "content": [{"type": "text", "text": "<冻结的正文、设定及本次需求>"}]
    }
  ]
}
```

结构化任务建议追加：

```json
"response_format": {"type": "json_object"}
```

本地代码同时支持向 user.content 追加多模态输入：

```json
{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<图片字节>"}}
```

```json
{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,<视频字节>"}}
```

这仅证明旧代码构造了上述格式；具体接入点支持的媒体格式、大小与上下文限制仍待验证。Demo 首先接通文本任务；图片结果优化需要看图时再验证图片输入。视频输入不增加为本版必做功能。

### 3.2 响应解析

| 字段 | 本地代码用法 | Demo 处理 |
|---|---|---|
| `choices[0].message.content` | 读取字符串；若为数组则拼接块中的 text | 校验非空及任务产出结构，再保存版本 |
| `usage.prompt_tokens` | 输入 token 数 | 保存原始用量，缺失记为未知 |
| `usage.completion_tokens` | 输出 token 数 | 保存原始用量，缺失记为未知 |
| 完整响应 | 旧代码主要输出文本 | 服务端保存必要回执，读取接口按权限裁剪 |

旧代码 HTTP 超时为 600 秒。这是旧执行器的等待设置，不是官方 SLA。Web 后端应以持久化任务异步执行，前端读取任务状态。

### 3.3 Demo 任务映射

| 任务 | 输入 | 交付条件 |
|---|---|---|
| 大纲优化、人物设定、剧本生成/优化 | 当前版本、绑定设定、需求 | 正文/设定保存成功，可追溯 |
| 分镜拆分 | 剧本版本、设定快照 | 解析为 Clip 结构；镜头时间、编号等校验通过 |
| 角色/场景/道具规划 | PRD 指定的 Clip 与设定 | 规范资产清单进入确认流程，不直接出图 |
| 图片 B 优化、内部 A+B 优化 | 对应方案及冻结上下文 | 方案版本或最终生图输入保存；隐藏 A 和内部文本不外传 |
| 视频方案生成/优化 | Clip、参考图及顺序快照 | 提示词保存；引用编号与冻结槽位一致 |

一个请求拆出多个 Clip 只记一个 attempt；结构不合格时保留失败记录。再次调用模型修复属于新的 attempt，不能隐去其费用。

## 4. Seedance：提交、查询与下载

### 4.1 创建任务

```http
POST /api/v3/contents/generations/tasks
```

由 `s_ark.py` 提炼的参考图生视频请求：

```json
{
  "model": "<所选档位的接入点>",
  "content": [
    {"type": "text", "text": "<已选视频方案版本的提示词>"},
    {
      "type": "image_url",
      "image_url": {"url": "data:image/jpeg;base64,<参考图1字节>"},
      "role": "reference_image"
    }
  ],
  "resolution": "720p",
  "ratio": "16:9",
  "duration": 5,
  "camera_fixed": false,
  "watermark": false,
  "generate_audio": true,
  "seed": -1
}
```

示例中的 5 秒仅用于说明字段；正式请求取所选模型已核验的参数。旧代码以 `-1` 请求随机 seed；Demo 须记录请求 seed 与供应商返回 seed，未返回实际 seed 时不能声称已经记录实际随机值。

参考图片按冻结槽位顺序追加到 `content`。由于第 0 项是文本，旧代码中的 `content[1]` 对应第一张参考图；错误定位按实际请求映射，不能把数组索引直接当资产 ID。

旧代码先把图片转 RGB/JPEG，长边不超过 1920、quality=88，再编码为 data URL。这是客户端处理策略，不是官方硬限制。若新后端保留该策略，应保留原图，并保存实际上传副本及 hash，保证重抽使用相同字节。

成功创建后读取响应 `id`，立即持久化为 `provider_task_id`；没有有效 ID 的响应不得标为已成功创建。

### 4.2 查询任务

```http
GET /api/v3/contents/generations/tasks/{provider_task_id}
```

使用创建时对应的账号凭证。本地脚本每 10 秒查询，最多等待 30 分钟，单次 HTTP 默认超时 90 秒；均为客户端设置。

| 供应商返回 | 本地执行器行为 | Demo 处理 |
|---|---|---|
| `status == succeeded` | 读取视频 URL 并下载 | 转入交付核验，尚不算最终完成 |
| `status` 为 `failed/cancelled/expired` | 保存失败响应 | 保存错误及已知用量；费用不能一律归零 |
| 其他状态 | 继续查询 | 保存原始状态；未知值不误判成功，完整枚举接入时复核 |
| 查询网络异常/本地等待超时 | 重试或退出脚本 | 保留任务 ID，稍后恢复查询，不重提生成 |

### 4.3 下载与交付

- 视频地址读取 `content.video_url`；用量读取 `usage.completion_tokens`。
- 下载到独立文件，完成后检查文件可定位、非空、可解码，并提取首帧缩略图。
- 原始供应商成功与本地交付成功分别记录；下载/解码失败不能把供应商生成状态改成失败。
- 下载失败只重试下载或重新查询现有任务，不创建新付费任务。URL 失效且无法恢复时明确标记交付失败。
- 每个结果节点只挂一个视频；存储路径不能仅使用可重复的业务名称，避免不同候选互相覆盖。

## 5. Seedream：接入点已提供，接口部分已核验

本次指定目录中核对到的图片流水线主要是 ComfyUI 和第三方执行器，不能作为 Seedream 官方请求合同。

用户已提供 `Doubao-Seedream-5.0-lite` 与 `Doubao-Seedream-5.0-pro` 的接入点，见 §2.1；默认选择 lite。两档单源图生图已实测通过，环境变量示例已同步；API Key 不写入本文，文生图、全部规格及两档长期凭证策略仍需接入阶段继续核验。

### 5.0 已完成的真实冒烟测试

2026-09-20 使用 `AI漫剧/剧情/师尊别砍我系统/00_静态资源/00人物-云轻-立绘.png` 作为单源图，提示词为“保持人物身份与服装结构，将背景改为干净的浅灰色，人物全身完整可见”，`n=1`、`size=1920x1920`、`response_format=url`、`watermark=false`。

| 档位 | HTTP | 响应中的实际 model | 用量 | 结果 |
|---|---:|---|---|---|
| lite | 200 | `doubao-seedream-5-0-260128` | `input_images=1`、`generated_images=1`、`output_tokens=14400` | `seedream-lite-smoke.png`，1920×1920 JPEG，302265 bytes |
| pro | 200 | `doubao-seedream-5-0-pro-260628` | `input_images=1`、`generated_images=1`、`output_tokens=14400` | `seedream-pro-smoke.png`，1920×1920 JPEG，315948 bytes |

结果文件 SHA-256：lite `3ae8a014bc077dcc56798c3a9a4b1c3b81247d79567ef55602a9d8aa65068d68`；pro `070aad28aa92bf3a45e836c1edf0ccc92bb6155320598abc14479c1f33e0dc8c`。

第一次用 `1024x1024` 请求被接口明确拒绝，返回“image size must be at least 3686400 pixels”；因此当前实测最低可用面积至少为 3,686,400 像素，本次使用 1920×1920。这个限制已经写入 Demo 的提交前校验候选，不能再默认 1024×1024。

实测只证明两把临时 Key、两个接入点和该请求形态可用；没有核验所有尺寸、比例、输出格式、费用字段或批量并发。建议本次验证后立即在方舟控制台轮换这两把已通过聊天传递的 Key。

### 5.1 最小实测顺序

先准备一个只含测试内容的目录和环境变量。不要把 Key 写进命令历史、脚本或文档：

```powershell
$env:ARK_BASE_URL = 'https://ark.cn-beijing.volces.com/api/v3'
$env:ARK_SEEDREAM_API_KEY = '从方舟控制台复制的 Key'
$env:ARK_SEEDREAM_LITE_MODEL = 'ep-20260920164208-q8z6z'
$env:ARK_SEEDREAM_PRO_MODEL = 'ep-20260920164144-9g86q'
```

先只测 lite 文生图，一次只生成 1 张、使用低成本的测试提示词：

```powershell
$body = @{
  model = $env:ARK_SEEDREAM_LITE_MODEL
  prompt = '一间空的国风书房，木桌，窗边柔和日光，无人物，无可读文字'
  size = '1024x1024'
  n = 1
  response_format = 'url'
  watermark = $false
} | ConvertTo-Json

$r = Invoke-WebRequest `
  -Uri "$env:ARK_BASE_URL/images/generations" `
  -Method Post `
  -Headers @{ Authorization = "Bearer $env:ARK_SEEDREAM_API_KEY" } `
  -ContentType 'application/json' `
  -Body $body

$r.StatusCode
$r.Content | ConvertFrom-Json | ConvertTo-Json -Depth 10
```

用 Python 验证更容易保留完整 UTF-8 响应和错误体：

```powershell
@'
import json, os, sys
from pathlib import Path
import requests

base = os.environ.get('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
key = os.environ['ARK_SEEDREAM_API_KEY']
model = os.environ.get('ARK_SEEDREAM_LITE_MODEL', 'ep-20260920164208-q8z6z')
payload = {
    'model': model,
    'prompt': '一间空的国风书房，木桌，窗边柔和日光，无人物，无可读文字',
    'size': '1024x1024',
    'n': 1,
    'response_format': 'url',
    'watermark': False,
}
r = requests.post(
    base + '/images/generations',
    headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
    json=payload, timeout=180,
)
print('HTTP', r.status_code)
try:
    data = r.json()
except ValueError:
    print(r.text[:2000]); sys.exit(1)
print(json.dumps({k: v for k, v in data.items() if k not in {'data'}}, ensure_ascii=False, indent=2))
if r.status_code >= 400:
    sys.exit(1)
items = data.get('data') or []
if not items:
    raise SystemExit('响应没有 data 产物')
print('RESULT_KEYS', sorted(items[0]))
print('RESULT_URL_OR_B64', 'url' in items[0], 'b64_json' in items[0])
Path('seedream-smoke-response.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
'@ | python -
```

预期成功证据是 HTTP 2xx、响应有 `data` 且包含 `url` 或 `b64_json`。如果返回 URL，立即下载并检查文件非空、能被图片库解码；不能只凭 HTTP 200 判定交付成功。

### 5.2 单源图生图

文生图成功后，再用一张本地 PNG 做第二次测试。图片字段的具体名称和 data URL/公网 URL 形式必须以该接入点返回的参数错误为准；常见方舟图片接口使用 `image` 字段：

```python
import os
import base64, mimetypes
from pathlib import Path

p = Path('test.png')
image = 'data:' + (mimetypes.guess_type(p.name)[0] or 'image/png') + ';base64,' + base64.b64encode(p.read_bytes()).decode()
payload = {
    'model': os.environ['ARK_SEEDREAM_LITE_MODEL'],
    'prompt': '保持人物身份与服装结构，将背景改成浅灰色',
    'image': image,
    'size': '1024x1024',
    'n': 1,
    'response_format': 'url',
    'watermark': False,
}
```

如果该接入点返回“参数不支持”或要求先上传文件，保留完整 4xx 响应，再按错误提示改成对应图片字段或上传流程；不要反复重发同一生成请求。成功后再用同一份 `test.png` 分别测试 pro，比较响应结构、耗时、实际用量和图片落盘结果。

### 5.3 实测记录

每次测试至少保存：模型接入点、请求时间、脱敏请求摘要、HTTP 状态、完整错误/响应元数据、结果文件 SHA-256、尺寸/格式、耗时、usage 与费用。Key、完整 data URL、隐藏提示词不得写入记录。

| 待核验项 | Demo 要求 |
|---|---|
| lite/pro 的账号权限与实际调用 | 按用户提供的模型与接入点映射验证，成功前不标记为 real 可用 |
| 官方请求路径、字段及响应形式 | 根据对应官方型号文档和一次成功调用补齐 |
| 文生图与单源图生图 | 都需要可用；联合派生分成两次独立生成 |
| 图片规格、seed、源图格式与大小 | 建立能力配置和提交前校验 |
| 用量与单价 | 明确计价单位、请求次数/产物数量及实际回执字段 |

mock 可以继续开发；Seedream 未接通时，PRD 的“真实图片→视频”全链路不得标为 real 已验收。

## 6. 规格、用量与费用

### 6.1 历史值，仅供迁移核对

| 项目 | 旧代码/README 的记录 | 当前使用口径 |
|---|---|---|
| 豆包价格 | 输入 3、输出 15 元/百万 tokens | 历史配置，非当前报价 |
| 视频价格 | mini 9.2、fast 27.75、2.5 70 元/百万 tokens | 历史配置，按账号及型号重新核对 |
| mini/fast 规格 | README 写为 480p/720p、4–15 秒 | 尚未独立核验 |
| 2.5 规格 | README 写为支持 1080p、4–30 秒 | 尚未独立核验 |
| 并发 | 旧代码注释写 3 | 账号限制待核验，不作为所有模型通用常量 |
| 参考图数量 | README 的分组工具为测试图片预留槽位 | 不能据此认定当前所有档位的正式上限 |

不把旧折扣、历史 token/秒估算、失败返回 0 元或“提交被拦必不扣费”当作当前计费合同。

### 6.2 Demo 费用记录

- token 计价模型的折算示例：`输入 tokens × 输入单价 / 1e6 + 输出 tokens × 输出单价 / 1e6`；视频若确认按输出 token 计价，则使用相应输出单价。
- 每个 attempt 冻结计价版本、币种和单位；区分提交前估算、根据返回用量折算、供应商账单确认金额。
- 用量或价格缺失记为未知/待核对，不默认为 0；失败任务也可能存在费用。
- 费用汇总按 attempt 去重，包括文本、资产规划及内部提示词优化。
- manager/admin 能读取费用；user 的响应不包含费用字段。mock 明确标识模拟，不计入实际支出。

## 7. 调用闭环与错误恢复

以下是新 Demo 的实现合同，不表示旧脚本已经实现。

1. **调用前校验**：正文/绑定关系完整；每项参考资产恰有一个未屏蔽可读结果；数量、时长、比例、分辨率和 seed 符合已核验能力。不支持时提示用户调整，不自动丢图或截断 Clip。
2. **冻结输入**：版本、需求、隐藏模板版本、模型规格、参考文件及顺序、hash、依赖、批次序号；不保存密钥。
3. **持久化提交身份**：同一次客户端网络重试复用相同操作 ID；新用户操作生成新 ID。服务端账本与执行锁防重复接受。
4. **外部调用**：每次实际模型调用独立 attempt。供应商未证实支持幂等键时，不假设加一个 header 就能保证幂等。
5. **响应丢失**：可能已接单但尚无任务 ID 时标为待核查；有 ID 就查询原任务。无法确定是否受理时不自动重提，尤其不能切账号/档位重发来“恢复”。
6. **完成与交付分离**：查询不新增生成 attempt；本地下载重试不新增模型费用记录。文本结构及媒体解码通过后才标记交付完成。
7. **部分失败**：N 次抽卡各有执行记录，成功结果保留。重新生成失败候选是明确的新付费操作，不能重跑整个成功批次。

鉴权、额度、参数或内容审核错误要保存供应商错误码及可展示的说明；日志与前端错误信息过滤凭证、隐藏提示词和大段 data URL。429 按服务端调度及供应商提示退避；POST 的 5xx/断网不得忽略“可能已受理”的情况。

## 8. 接入验收清单

- [x] 收录用户提供的 Seedream 5.0 lite/pro 模型名称与接入点，并同步配置示例。
- [ ] 控制台核验豆包、Seedance、Seedream 的模型映射、凭证权限、规格、限流与计价；Seedream 补齐请求与响应合同。
- [ ] mock 全链路运行且不调用外部模型。
- [ ] real 豆包：一次文本请求，保存有效结果、输入快照和原始 usage。
- [ ] real Seedream：文生图、单源图生图能落盘和解码。
- [ ] real Seedance：一个 Clip 完成参考校验、方案生成、创建任务、恢复查询、下载和首帧提取。
- [ ] 重复提交复用任务；响应不明不盲目重发；下载失败仅恢复交付；批次部分失败不覆盖成功结果。
- [ ] 超限参考图/时长等在外部调用前拦截；缺失用量显示未知。
- [ ] 页面/API 不泄露 Key、隐藏 A 或完整内部优化文本；user 无计费字段。
- [ ] 刷新/后端重启后能从已保存任务 ID 继续跟踪，旧方案仍使用旧参考快照。

每次 real 验证记录：日期、模型/接入点、脱敏配置版本、操作及 attempt ID、供应商任务/请求 ID（若有）、结果 hash、交付状态和用量。本文不以静态代码核对代替实测验收。
