# 一键在你自己的机器上启动「漫剧工作台」本地验收环境（mock 模式，不花钱）。
#
# 用法（在仓库根目录执行）：
#   powershell -ExecutionPolicy Bypass -File .\scripts\start_local.ps1
# 可选参数：
#   -BackendPort 8010   -WebPort 5173   -SkipFrontend
#
# 该脚本只做「起服务」：依赖安装与门禁请先按 docs/DEVELOPMENT.md §3 跑
#   .\scripts\verify_clean_env.ps1
param(
    [int]$BackendPort = 8010,
    [int]$WebPort = 5173,
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'

Write-Host "仓库根目录：$root" -ForegroundColor Cyan

# --- 前置校验：依赖是否已就位 -------------------------------------------------
$pylibs = Join-Path $backend '.pylibs'
if (-not (Test-Path $pylibs)) {
    Write-Host "缺少 $pylibs —— 先跑：" -ForegroundColor Red
    Write-Host "  python -m pip install --target backend\.pylibs -r backend\requirements.lock"
    exit 1
}
if (-not (Test-Path (Join-Path $backend '.env'))) {
    Write-Host "缺少 backend\.env —— 先跑：Copy-Item backend\.env.example backend\.env" -ForegroundColor Red
    exit 1
}

# --- 后端：mock 模式，启动时自动建库 + 迁移 + seed 管理员 ---------------------
# 强制 mock：backend\.env 里有真实火山方舟凭证，切 real 会真花钱。
$env:APP_MODE = 'mock'
$env:WORKER_ENABLED = 'true'      # 生成任务必开；只看界面可以设 false
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = "$pylibs;$backend"
Remove-Item Env:\DEBUG -ErrorAction SilentlyContinue

Write-Host "`n[后端] http://127.0.0.1:$BackendPort  (APP_MODE=mock, 不发起任何外部请求)" -ForegroundColor Green
Write-Host "       接口文档 http://127.0.0.1:$BackendPort/docs   (Ctrl+C 停止)"
Start-Process -FilePath 'python' `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', $BackendPort `
    -WorkingDirectory $backend -NoNewWindow

# --- 前端：显式绑 127.0.0.1，代理指到上面的后端 -------------------------------
if (-not $SkipFrontend) {
    if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
        Write-Host "缺少 frontend\node_modules —— 先跑：npm ci --prefix frontend" -ForegroundColor Red
        exit 1
    }
    Start-Sleep -Seconds 3   # 给后端一点时间做迁移与 seed
    Write-Host "[前端] http://127.0.0.1:$WebPort  (登录 admin / admin123)" -ForegroundColor Green
    $env:MANJU_BACKEND = "http://127.0.0.1:$BackendPort"
    $env:MANJU_WEB_PORT = "$WebPort"
    Start-Process -FilePath 'node' `
        -ArgumentList 'node_modules\vite\bin\vite.js', '--host', '127.0.0.1', '--port', $WebPort, '--strictPort' `
        -WorkingDirectory $frontend -NoNewWindow
}

Write-Host "`n已启动。健康检查： http://127.0.0.1:$BackendPort/api/v1/health" -ForegroundColor Cyan
Write-Host "跑演示链路：`n  `$env:MANJU_BACKEND_PORT='$BackendPort'; python backend\scripts\seed_demo.py --user admin --password admin123" -ForegroundColor Cyan
