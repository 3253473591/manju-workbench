# 开发与复现

环境要求、从零复现路径（门禁 G1–G8）、常用脚本与测试口径。
访客向总览（产品是什么、界面、关键设计与已知边界）见 [`README.md`](../README.md)。

---

## 2. 环境要求

**下面这组版本是唯一被验证过的组合**：仓库内 [`docs/acceptance.md`](docs/acceptance.md)
和仓库外的复核稿 `2026-09-21 干净环境复核.md` 记的结论（117 passed、构建通过、迁移无漂移）
只对这组版本负责；换了版本请自己重跑一遍再引用结论。

| 组件 | 验证版本（实测） | 检查命令 | 说明 |
|---|---|---|---|
| Python | 3.13.x（3.13.9） | `python --version` | 依赖装在工作区 `backend\.pylibs`，不污染系统 Python |
| MySQL | 8.0（8.0.44） | `python backend\scripts\check_env.py` | 库名 `manju_director`（不存在会自动创建）；脚本连上去读 `SELECT VERSION()`，不需要装 mysql 命令行客户端 |
| Node | 24.x（24.18.0） | `node --version` | 前端构建；具体包版本由 `frontend/package-lock.json` 锁定 |
| npm | 随 Node | `npm --version` | 安装用 `npm ci`，**不要用 `npm install`**（后者会改 lockfile） |
| ffmpeg / ffprobe | 任意较新版 | `ffmpeg -version`、`ffprobe -version` | 视频落盘核验与首帧缩略图；**mock 模式也要它们真的编码 MP4、抽首帧** |
| Google Chrome | 任意较新版 | — | 仅用于 `scripts/shot.py` 截图 |

一条命令把上面这些全部检查掉（只用标准库，**依赖还没装也能跑**）：

```powershell
python backend\scripts\check_env.py
```

它逐项打印 `OK / WARN / FAIL`：Python、Node/npm、ffmpeg/ffprobe、后端依赖能否
`import fastapi, sqlmodel, alembic`、应用库与测试库的 MySQL 连接、端口占用。
任何 `FAIL` 都直接给出「怎么修」并以退出码 1 结束——**先把 FAIL 清掉，再判断是不是代码问题**，
省得把版本问题、依赖问题当成业务 bug 查。

MySQL 账号要求：文档和 `.env.example` 里的 `root:root` 只是示例占位。
你需要一个能 **CREATE DATABASE、执行 Alembic 迁移、TRUNCATE 表** 的账号；
密码不同就改 `backend\.env` 的 `DATABASE_URL`（应用库）和 `MANJU_TEST_DATABASE_URL`（测试库）。

ffmpeg 安装（Windows）：`winget install Gyan.FFmpeg` 或 `choco install ffmpeg`；
也可以下静态包解开后把 `bin` 目录加进 PATH，**重开终端**再用 `ffmpeg -version` 确认。

---


## 3. 从零开始的最短复现路径

技术负责人第一次 clone 后按这个顺序走：任何一项失败都先修环境或入口，不进入业务深测。

| 门禁 | 目标 | 通过条件 |
|---|---|---|
| G0 | 干净 clone | 工作区没有 `backend\.pylibs`、`frontend\node_modules` 和运行期数据时仍能开始安装 |
| G1 | Python 依赖 | 按锁定文件安装后 `python -c "import fastapi, sqlmodel, alembic"` 成功 |
| G2 | 前端依赖 | `npm ci --prefix frontend` 成功（lockfile 已入库，不需要先跑 `npm install`） |
| G3 | 基础设施 | MySQL 8 连接串与 ffmpeg/ffprobe 都有可执行的检查命令，失败时给出修法 |
| G4 | 数据库初始化 | 启动服务能建库 → 跑 Alembic → 写 seed；缺权限时错误指向具体配置 |
| G5 | 服务启动 | mock 模式下 `/docs` 与健康检查可访问，端口/`PYTHONPATH`/`.env` 规则与本文一致 |
| G6 | 后端测试 | 独立 `*_test` 库，`python -m pytest` 稳定得到 **117 passed**，且不读 `DATABASE_URL` 污染演示库 |
| G7 | 前端构建 | `npm run build --prefix frontend` 成功 |
| G8 | 回归证据 | README、`docs/how-to-test.md` 与复核稿的命令、版本、库规则完全一致 |

### 3.1 手工路径（每一步都标了当前目录）

```powershell
# ---- 当前目录：任意 ----
git clone <repo-url>
Set-Location manju-director

# ---- 当前目录：仓库根目录；G3 版本与基础设施自检 ----
python --version                                   # 期望 3.13.x
node --version                                     # 期望 v24.x
ffmpeg -version
ffprobe -version
python backend\scripts\check_env.py                # 版本 + ffmpeg + MySQL + 端口，一次跑完

# ---- G1 后端依赖：按锁定文件装进工作区，不写系统 Python ----
python -m pip install --target backend\.pylibs -r backend\requirements.lock

# ---- G2 前端依赖：用 ci 而不是 install ----
npm ci --prefix frontend

# ---- G4 配置：.env 只管应用；测试库走独立环境变量 ----
Copy-Item backend\.env.example backend\.env
# 然后编辑 backend\.env 的 DATABASE_URL，填你自己的账号密码

# ---- G6 后端测试：当前目录切到 backend ----
$env:MANJU_TEST_DATABASE_URL = "mysql+pymysql://<user>:<password>@127.0.0.1:3306/manju_director_test?charset=utf8mb4"
Set-Location backend
$env:PYTHONPATH = "$(Resolve-Path .\.pylibs);$(Resolve-Path .)"
$env:PYTHONUTF8 = "1"
$env:DEBUG = $null
python -m pytest                                   # 期望：117 passed
Set-Location ..

# ---- G7 前端构建：当前目录仓库根目录 ----
npm run build --prefix frontend

# ---- G5 起服务：当前目录仓库根目录 ----
$env:APP_MODE = "mock"                             # 不发任何外部请求
$env:WORKER_ENABLED = "false"                      # 只看界面时可以关掉后台 worker
$env:PYTHONPATH = "$(Resolve-Path .\backend\.pylibs);$(Resolve-Path .\backend)"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

**测试库的三条硬规则**（`backend/tests/conftest.py` 强制，不是建议）：

1. 测试只读 `MANJU_TEST_DATABASE_URL`（不设就用内置默认 `.../manju_director_test`），
   **不读 `DATABASE_URL`**——那份是给应用跑的，混用会把演示库 drop 掉；
2. 库名必须以 `_test` 结尾，否则直接拒绝运行；
3. 账号要能建库、跑迁移、清表；两个 pytest 会话跑同一个库时会被会话级 `GET_LOCK` 串行化。

### 3.2 一条命令跑完门禁（推荐，等价于 3.1）

```powershell
.\scripts\verify_clean_env.ps1
# 已经被占用/已装好的环境只想复核：
.\scripts\verify_clean_env.ps1 -SkipInstall -SkipFrontendBuild
```

脚本用绝对路径 + 显式工作目录执行，**不依赖当前目录**，逐步打印 G1–G8 结论并在最后汇总。
它比手工路径多两件事：强制走 `requirements.lock`、核对测试总数是不是 117
（数量对不上说明代码或文档有一方过期）。测试库连接串从 `MANJU_TEST_DATABASE_URL` 读；
没设就用 `conftest.py` 的内置默认值跑，并在汇总里 WARN 提醒你显式设置
（本机 MySQL 不是 `root:root` 的话，`check_env.py` 会直接 FAIL 并指出是凭证问题）。
`powershell -ExecutionPolicy Bypass -File .\scripts\verify_clean_env.ps1` 可绕开执行策略限制。

### 3.3 安装与运行（拆开看）

#### 后端

```powershell
# 1) 装依赖到工作区内（避免写入系统 Python 目录）
python -m pip install --target backend\.pylibs -r backend\requirements.lock

# 2) 配置：复制 .env.example 为 .env 并填写
Copy-Item backend\.env.example backend\.env
#    APP_MODE=mock 时不发任何外部请求；填好 ARK_* 后可改为 real

# 3) 起服务（启动时自动建库 → 跑迁移 → 种子管理员与模型配置）
$env:PYTHONPATH="$(Resolve-Path .\backend\.pylibs);$(Resolve-Path .\backend)"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

- 接口文档：<http://127.0.0.1:8010/docs>
- 健康检查：<http://127.0.0.1:8010/api/v1/health>
- 首次启动会 seed 管理员 `admin / admin123`（可用 `BOOTSTRAP_ADMIN_*` 覆盖，之后不再自动覆盖）

#### 前端

```powershell
npm ci --prefix frontend                # 按 lockfile 安装，可复现
npm run dev --prefix frontend           # 或 node frontend\node_modules\vite\bin\vite.js
```

- 地址：<http://127.0.0.1:5173>（Vite 显式绑定 `127.0.0.1`）
- 端口被占用时可覆盖：`MANJU_BACKEND=http://127.0.0.1:8020`、`MANJU_WEB_PORT=5180`
- 新版 npm 会打印 `allow-scripts ... esbuild ... postinstall` 的警告：**可以忽略**——
  esbuild 的平台二进制走 `optionalDependencies`，`npm ci` 之后已经就位
  （干净目录实测：169 个包、`node node_modules\esbuild\bin\esbuild --version` 正常输出）。

#### 跑一遍完整链路（演示数据）

```powershell
$env:MANJU_BACKEND_PORT=8010
python backend\scripts\seed_demo.py --user admin --password admin123
```

脚本会走完 大纲 → 设定 → 剧本 → 3 个 Clip → 资产规划确认 → 出图 → 立绘联合派生 →
视频方案 → 视频抽卡，并打印项目地址。

---


## 4. 常用脚本

| 脚本 | 用途 |
|---|---|
| `scripts/check_env.py` | **启动前环境预检**：Python/Node/ffmpeg/依赖导入/MySQL/端口，逐项给修法 |
| `scripts/db_init.py` | 建库 + `create_all`（快速自检，正式建表走 Alembic） |
| `scripts/db_reset.py --yes` | 清空目标库 |
| `scripts/export_openapi.py` | 导出服务端生成的 OpenAPI 3.1 |
| `scripts/validate_openapi.py` | 校验手写契约（结构 + `$ref` 解析） |
| `scripts/smoke_real.py` | real 模式逐能力冒烟（文本 / 文生图 / 图生图 / 视频） |
| `scripts/mark_verified.py` | 把真实冒烟结果写回模型核验时间与实际模型 |
| `scripts/seed_demo.py` | 通过 HTTP 跑通完整链路，准备演示数据 |
| `scripts/continue_real_demo.py` | 从「已有剧本」续跑 real 剩余步骤（真实文本调用很慢，易超时） |
| `scripts/inspect_canvas.py` | 打印某集画布布局包围盒（排查排版问题） |
| `scripts/inspect_submissions.py` | 打印某集的提交与 attempt 耗时、状态、用量 |
| `scripts/recalc_usage.py` | 按当前计价口径重算历史费用（调价 / 修正口径后用，不必重跑生成） |
| `scripts/shot.py` | 用 Chrome CDP 截图（可注入 localStorage 会话与执行 JS） |

### Alembic

```powershell
# 当前目录：backend
$env:PYTHONPATH = "$(Resolve-Path .\.pylibs);$(Resolve-Path .)"
python -m alembic -c alembic.ini upgrade head
python -m alembic -c alembic.ini downgrade base
python -m alembic -c alembic.ini check          # 期望输出 No new upgrade operations detected
```

迁移读写 `DATABASE_URL`（应用库配置）；要在测试库上验漂移就临时
`$env:DATABASE_URL = $env:MANJU_TEST_DATABASE_URL`，**必须对着已经迁到 head 的库跑**，
对遗留演示库跑会报历史漂移，那不代表仓库有问题。

也可以走统一入口 `backend\run.cmd`，它替你设好 `PYTHONPATH` 与 UTF-8：

```powershell
backend\run.cmd -m alembic -c alembic.ini check
```

### 测试

```powershell
# 当前目录：backend
$env:MANJU_TEST_DATABASE_URL = "mysql+pymysql://<user>:<password>@127.0.0.1:3306/manju_director_test?charset=utf8mb4"
$env:PYTHONPATH = "$(Resolve-Path .\.pylibs);$(Resolve-Path .)"; $env:PYTHONUTF8 = "1"; $env:DEBUG = $null
$env:APP_MODE = "mock"; $env:WORKER_ENABLED = "false"
python -m pytest                       # 期望：117 passed
```

- **命令里不要再加 `-q`**：`pytest.ini` 的 `addopts` 已经有 `-q`，再加一层会把结尾的
  `117 passed` 汇总行也压掉，你就只剩一排点，没法一眼核对数量。
- 测试库由 `conftest.py` 强制指定：**只读 `MANJU_TEST_DATABASE_URL`，不读 `DATABASE_URL`**；
  库名必须以 `_test` 结尾，否则拒绝运行。
- 不想手动设环境变量就用 `.\scripts\verify_clean_env.ps1`（它会替你核对 117 这个数）。
- 更细的口径（并发串行化、迁移漂移怎么跑、计费怎么核）见
  [`docs/how-to-test.md`](docs/how-to-test.md)。

---

