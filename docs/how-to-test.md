# 怎么测

三条路径，按你想花的时间挑：

- **A. 手工点一遍**（推荐，5 分钟）——服务已经在跑，打开浏览器就行
- **B. 自动化跑一遍**（2 分钟）——测试套件 + 端到端脚本
- **C. 按 PRD 验收清单逐项验**（20 分钟）——十项，每项点哪、看什么，见 §4

---

## 1. 现在的运行状态

| 组件 | 地址 | 状态 |
|---|---|---|
| 前端 | <http://127.0.0.1:5180> | 运行中 |
| 后端 | <http://127.0.0.1:8020/api/v1> | 运行中，`APP_MODE=mock` |
| 接口文档 | <http://127.0.0.1:8020/docs> | Swagger UI，可直接试接口 |
| MySQL | `127.0.0.1:3306/manju_director` | 运行中 |

**登录账号**：`admin` / `admin123`（本地 Demo 账号，首次启动自动 seed）

> 端口说明：`8010`/`5173` 被遗留的旧项目 dev server 占着，我没动它们，
> 新栈用 `8020`/`5180`。想换端口见 §6。

---

## 2. A. 手工点一遍（最短路径）

打开 <http://127.0.0.1:5180> → 用 `admin` / `admin123` 登录。

左侧已经有好几个演示项目，**随便点一个**进去就是生产画布。建议按这个顺序点：

| # | 操作 | 应该看到什么 |
|---|---|---|
| 1 | 点左侧任意项目的名字 | 进入画布，右侧抽屉自动选中「大纲」节点并显示正文 |
| 2 | 看画布 | 一条从左到右的流水线：大纲 → 剧本 → 多个 Clip → 图片方案 → 图片结果 → 视频方案 → 视频结果 |
| 3 | 点「适应画布」 | 整条流水线缩放到可见；点「回到起点」回到可读缩放 |
| 4 | 点任意 **Clip 节点** | 抽屉显示 Clip 正文（含镜头 1/2 的起止时间）、参考资产、以及「发送 / 生成场景 / 生成道具 / **生成视频方案**」 |
| 5 | 点 **视频方案节点**（紫色带播放图标旁边那个） | 抽屉显示结构化提示词（参考图定义段 + `镜头N（起-止秒）` 分镜段 + 无字幕收尾）、冻结的参考图缩略图、冻结的 Clip |
| 6 | 点 **视频结果节点** | 抽屉里有可播放的视频播放器 |
| 7 | 拖动任意节点 / 拉右下角改尺寸 | 松手后布局写库；刷新页面位置还在 |
| 8 | 在画布空白处双击 | 弹出导入文件对话框 |
| 9 | 切到「资产库」 | 每个资产的「可用结果」列：显示 `1（可出视频）` 才算通过视频前置校验 |
| 10 | 切到「任务记录」 | 每条提交的操作、状态、错误；点进去能看到 attempt 的耗时与用量 |
| 11 | 切到「费用账单」 | 实际支出 / 预估 / mock 模拟 / 待核对条数分开显示 |
| 12 | 切到「模型配置」 | 6 个档位的接入点、核验状态、计价；未核验的显式标橙 |

**自己新建一集试试**（完整链路约 1 分钟，mock 下几乎瞬时）：

1. 左上「＋ 新建项目」→ 填名字 → 创建
2. 抽屉里在大纲框写点东西 → 「保存为版本」
3. 「人物设定」→ 等几秒 → 抽屉的「任务反馈」变成 succeeded
4. 「生成剧本」→ 画布上多出一个剧本节点
5. 点剧本节点 → 「分镜」→ 拆出 3 个 Clip 节点
6. 点剧本节点 → 「人物立绘」→ 任务完成后，抽屉底部出现**资产规划清单** → 点「确认清单」
7. 画布上出现图片方案节点 → 点它 → 「抽卡出片」→ 出现图片结果
8. 点那个图片结果 → 「生成面部特写与三视图」→ 又出 2 张（自动屏蔽多余候选）
9. 点 Clip 节点 → 「生成视频方案」→ 出现视频方案节点
10. 点视频方案 → 「抽卡出片」→ 出现视频结果，点开抽屉里有播放器

> 每一步都在抽屉底部的「任务反馈」里能看到 `操作 · 状态 · N 次实际调用`。
> 如果某步失败，会显示错误码和中文说明（比如参考资产没凑齐时点名是哪个资产）。

---

## 3. B. 自动化跑一遍

### 3.1 测试套件（79 项）

```powershell
cd D:\AAA_Code_Project\漫剧工作台\manju-director\backend
$env:PYTHONPATH="$PWD\.pylibs;$PWD"
$env:PYTHONUTF8='1'
$env:DATABASE_URL='mysql+pymysql://root:root@127.0.0.1:3306/manju_director_test?charset=utf8mb4'
$env:APP_MODE='mock'
$env:WORKER_ENABLED='false'
python -m pytest -q
```

测试库是独立的 `manju_director_test`，每个用例前清表，**不会动你的演示数据**。

想看细节加 `-v`，想看某个文件：`python -m pytest tests/test_mock_chain.py -v`

### 3.2 端到端链路脚本

```powershell
$env:MANJU_BACKEND_PORT='8020'
python scripts\seed_demo.py --name "我自己的测试"
```

会真的走一遍大纲 → 设定 → 剧本 → 分镜 → 资产 → 出图 → 派生 → 视频方案 → 视频，
每一步打印耗时和 id，最后给出前端地址。

### 3.3 契约 / 迁移 / 前端类型

```powershell
python scripts\validate_openapi.py                      # 手写契约的结构与 $ref
python scripts\export_openapi.py                        # 导出服务端实际生成的 OpenAPI
python -m alembic -c alembic.ini check                  # 应输出 No new upgrade operations
node ..\frontend\node_modules\typescript\bin\tsc -p ..\frontend\tsconfig.json
```

### 3.4 验计费算得对不对

```powershell
python -m pytest tests/test_pricing.py -v        # 用官方报价逐项核对折算
python scripts\recalc_usage.py --dry-run         # 按当前单价重算历史账，只看差异
```

`tests/test_pricing.py` 里有这些断言，可以直接对着报价表核：

| 场景 | 期望 |
|---|---|
| 豆包 输入 1000 / 输出 2000 | `1000×3/1e6 + 2000×15/1e6 = 0.033` |
| seedance mini 108,900 tokens（不含视频输入） | `2.5047` |
| seedance mini 同上（含视频输入） | `1.5246` |
| seedance 2.5 100,000 tokens @720p / @1080p | `7` / `7.7` |
| seedream lite 4 张 | `0.88` |
| seedream pro 1920×1920 出图 1 张、输入图 1 张 | `0.6`（首张输入免费 + 大像素档） |
| seedream pro 输入图 2 张 | `0.62` |

想改单价：改 `backend/app/db/seed.py` 里的 `PRICING_*` 常量 → 重启后端（会自动同步到库）
→ `python scripts\recalc_usage.py` 重算历史账。

---

## 4. C. 按 PRD §9 验收清单逐项验

每一项都给「怎么做」和「看什么」。[`docs/acceptance.md`](acceptance.md) 里有对应的自动化证据。

| # | 验收项 | 手工怎么验 |
|---|---|---|
| 1 | 新建项目→大纲→人物设定 | 新建项目后大纲框写入 → 保存为版本 → 点「人物设定」→ 看任务反馈成功 |
| 2 | 生成剧本、拆 2–3 个 Clip、可追溯 | 点「生成剧本」「分镜」→ 点剧本节点看正文上方绑定的版本；点 Clip 看内部镜头的时间轴 |
| 3 | 确认资产清单、图片复用 | 点「人物立绘」→ 抽屉里出现清单 → 确认 → 再对同一个资产确认一次，看返回的是**复用**而不是新建 |
| 4 | 立绘联合派生两张、旧源图不变 | 抽 2 张立绘 → 分别点「生成面部特写与三视图」→ 两批结果都在，旧批次内容不变 |
| 5 | 屏蔽多余候选才过视频校验 | 资产库里把「可用结果」弄到多于 1 → 点 Clip 的「生成视频方案」→ **会被拦下并点名资产**；屏蔽到剩 1 → 通过 |
| 6 | 视频方案、选版、抽卡、可播放 | 点视频方案 → 看结构化提示词与冻结参考图 → 「保存方案版本」/「选版」→ 「抽卡出片」→ 结果节点里有播放器，缩略图是首帧 |
| 7 | 改 Clip 再生成、旧方案仍用旧上下文 | 点 Clip 改标题保存 → 再点「生成视频方案」→ 得到**新**方案节点；点回**旧**方案节点，它显示的冻结 Clip 标题仍是旧的 |
| 8 | 版本与来源、角色费用差异 | 用 `manager` 账号登录（见下），画布全只读、能看「费用账单」；用 `user` 账号登录，看不到「费用账单」菜单 |
| 9 | 刷新后状态还在 | 拖几个节点 → F5 → 位置、版本、引用、已交付媒体都还在 |
| 10 | mock 无外部请求 / real 已验证 | 当前是 `mock`，右上角标签会写「mock 模式」；real 的验证记录见 [`docs/real-verification.md`](real-verification.md) |

### 4.1 造一个 manager / user 账号来验权限

在「用户管理」里：注册一个新账号（登录页「注册」页签），然后用 admin 在
「用户管理」把它改成 `manager`。切到那个账号登录，就能看到只读视角。

**要验的重点**：manager 的所有写操作在后端是 403（不是前端藏按钮）——
可以用 `/docs` 直接拿 manager 的 token 调 `POST /projects` 试试。

---

## 5. 故意把它弄坏（验错误处理）

这些是「应该优雅失败」的路径，值得手动试：

| 试法 | 期望 |
|---|---|
| 生成类请求不带 `Idempotency-Key` | 400 `INVALID_REQUEST`，不是 500 |
| 同一个 Idempotency-Key 换不同请求体 | 409 `IDEMPOTENCY_CONFLICT` |
| 同一个节点连点两次「发送」 | 第二次 409 `GENERATION_IN_PROGRESS`（服务端锁，不是按钮禁用） |
| 保存文档时带过期的 `expectedVersion` | 409 `CONTENT_VERSION_CONFLICT`，details 里有实际版本号 |
| 把 Clip 时长改成 20 秒，再用 mini 出片 | 提交前就失败 `MODEL_NOT_AVAILABLE`，提示 4–15 秒，**没有产生 attempt** |
| 参考资产没凑齐时点「生成视频方案」 | 422 `ASSET_REFERENCE_INVALID`，**点名**是哪个资产差在哪 |
| 导入一张不按命名规范的图片 | 422 并提示按 `00人物-张三-冬装-三视图` 规范命名或手动指定名称 |
| 用没权限的账号调管理接口 | 403 `FORBIDDEN` |

---

## 6. real 模式怎么切

凭证已经在 `backend/.env` 里了（只落这一处，已 gitignore）。

```powershell
# 方法一：临时环境变量（推荐，测完就没了）
cd backend
$env:PYTHONPATH="$PWD\.pylibs;$PWD"
$env:APP_MODE='real'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8020

# 方法二：改 backend\.env 里的 APP_MODE=real，然后重启后端
```

切过去之后右上角会变成红色「real 模式」。**注意会真花钱**，参考单价（个人户）：
s-mini 0.200 元/秒、fast 0.602、2.5 720P 1.518 / 1080P 3.41。

想单独验某一个能力、不动数据库，用冒烟脚本：

```powershell
$env:APP_MODE='real'
python scripts\smoke_real.py --text                          # 豆包文本
python scripts\smoke_real.py --image                         # Seedream 文生图（lite + pro）
python scripts\smoke_real.py --image-to-image <本地图片路径>   # 单源图生图
python scripts\smoke_real.py --video <本地参考图路径>          # Seedance 图生视频
```

跑完整 real 链路（会慢，文本单次 70–190 秒）：

```powershell
python scripts\seed_demo.py --name "real 测试"
python scripts\continue_real_demo.py <projectId>    # 文本步骤超时了用它续跑
```

**测完记得切回 mock**，否则每次点击都在花钱。

---

## 7. 重开服务 / 换端口

```powershell
# 后端
cd D:\AAA_Code_Project\漫剧工作台\manju-director\backend
$env:PYTHONPATH="$PWD\.pylibs;$PWD"; $env:PYTHONUTF8='1'; $env:APP_MODE='mock'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8020

# 前端（必须显式绑 127.0.0.1，否则可能只监听 IPv6 连不上）
cd ..\frontend
$env:MANJU_BACKEND='http://127.0.0.1:8020'; $env:MANJU_WEB_PORT='5180'
node node_modules\vite\bin\vite.js --host 127.0.0.1 --port 5180 --strictPort
```

- 后端启动时会自动：建库（缺则建）→ 跑 Alembic 迁移 → seed 管理员与模型配置
- **清空重来**：`$env:DATABASE_URL='...manju_director...'` 后跑 `python scripts\db_reset.py --yes`，
  再重启后端即可（演示数据会没，管理员会重新 seed）
- 换端口时前端和后端要**一起改**：前端靠 `MANJU_BACKEND` 决定代理目标

---

## 8. 已知会让你困惑的点

| 现象 | 原因 |
|---|---|
| 右上角写「real 未核验」 | 有档位没通过真实调用核验（fast / 2.5）。这是**故意的**，未核验不许显示为可用 |
| 画布默认只显示前两个节点 | 流水线有几万像素宽，硬 fit 会把字缩到看不见。用「适应画布」看全图，「回到起点」回到可读缩放 |
| 费用显示「待核对」而不是金额 | 该档位没登记计价口径，或用量缺失。**单价缺失时记未知而不是 0**——0 会被读成免费。当前 6 个档位都登记了官方报价，正常情况应显示具体金额 |
| 左边项目越点越多 | 我跑演示脚本建的，纯测试数据，可以不管 |
| 手动「添加节点」里没有「新建文本」 | 文字节点来自导入或生成；节点是业务对象的视图，不能凭空建 |
| 视频提示词下面有黄色告警框 | 产线规范 lint 发现缺项（分镜标签/参考图编号/无字幕等）。**故意不拦死**，因为手册说「判一版至少跑 2 发」 |

---

## 9. 想让我继续做的方向

1. 把 `经验教训.md`（80KB，手册列为「最优先」）逐条吸收进系统提示词与校验规则
2. 阶段门禁状态机（手册要求「每阶段产出必须用户确认才能进下一步」）
3. 真人写实项目的处理决策（官方会拦，手册的出路是中转，但 PRD 说只接官方）
4. 接入手册里的 m（本地 ComfyUI 免费草稿层），降低试错成本
