#Requires -Version 5.1
<#
.SYNOPSIS
    漫剧工作台：从零复现门禁（G0–G8）。

.DESCRIPTION
    把 docs/DEVELOPMENT.md §3「从零开始的最短复现路径」里散落的步骤收成一条命令，逐项打印结论。

    与 README 手工路径等价；区别只是它会强制走**锁定依赖**、**独立 *_test 库**，
    并在最后核对测试总数与文档是否一致。

    任何一步失败都会立刻打印「哪一步、为什么、怎么修」，然后继续跑后面的步骤，
    最后给出总表并以退出码 1 结束。

    脚本不依赖当前目录：所有命令都用绝对路径 + 显式工作目录执行。

.PARAMETER TestDatabaseUrl
    测试库连接串。默认取 $env:MANJU_TEST_DATABASE_URL，再回退到 conftest.py 的内置默认值。
    库名必须以 _test 结尾，否则测试会拒绝运行（这是故意的，防止毁掉演示库）。

.PARAMETER ExpectedPassed
    期望的用例通过数，默认 117。数量对不上说明代码或文档有一方过期。

.EXAMPLE
    .\scripts\verify_clean_env.ps1

.EXAMPLE
    .\scripts\verify_clean_env.ps1 -SkipInstall -SkipFrontendBuild

.NOTES
    只用 Windows PowerShell 5.1 就有的语法；在 PowerShell 7 上同样可用。
#>
[CmdletBinding()]
param(
    [string]$TestDatabaseUrl = $env:MANJU_TEST_DATABASE_URL,
    [string]$Requirements = 'backend\requirements.lock',
    [int]$ExpectedPassed = 117,
    [switch]$SkipInstall,
    [switch]$SkipFrontendBuild,
    [switch]$SkipTests,
    [switch]$SkipAlembic,
    [switch]$Lenient
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Settings 由 Pydantic 按“进程环境变量优先于 .env”读取。清掉可能来自宿主
# shell/CI 的同名变量，保证门禁验证的是仓库配置，而不是调用者的残留状态。
$script:InheritedEnvironment = @{
    DEBUG = [Environment]::GetEnvironmentVariable('DEBUG', 'Process')
    DATABASE_URL = [Environment]::GetEnvironmentVariable('DATABASE_URL', 'Process')
}
[Environment]::SetEnvironmentVariable('DEBUG', $null, 'Process')
[Environment]::SetEnvironmentVariable('DATABASE_URL', $null, 'Process')

try {

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $RepoRoot 'backend'
$Frontend = Join-Path $RepoRoot 'frontend'
$Pylibs = Join-Path $Backend '.pylibs'
$RequirementsPath = if ([System.IO.Path]::IsPathRooted($Requirements)) {
    $Requirements
} else {
    Join-Path $RepoRoot $Requirements
}
$DefaultTestDatabaseUrl = 'mysql+pymysql://root:root@127.0.0.1:3306/manju_director_test?charset=utf8mb4'
$UsingDefaultTestUrl = [string]::IsNullOrWhiteSpace($TestDatabaseUrl)
if ($UsingDefaultTestUrl) { $TestDatabaseUrl = $DefaultTestDatabaseUrl }

if (-not (Test-Path (Join-Path $Backend 'app\main.py'))) {
    Write-Host "找不到 backend\app\main.py；请从仓库根目录运行，或确认脚本在 <repo>\scripts\ 下。" -ForegroundColor Red
    exit 2
}

# --- 结果收集 ---------------------------------------------------------------

$script:Results = [System.Collections.Generic.List[object]]::new()

function Add-Result {
    param(
        [string]$Gate,
        [string]$Name,
        [ValidateSet('PASS', 'FAIL', 'SKIP', 'WARN')][string]$Status,
        [string]$Detail = '',
        [string[]]$Fix = @()
    )
    $script:Results.Add([pscustomobject]@{
            Gate   = $Gate
            Name   = $Name
            Status = $Status
            Detail = $Detail
            Fix    = $Fix
        })
    $color = switch ($Status) {
        'PASS' { 'Green' }
        'FAIL' { 'Red' }
        'WARN' { 'Yellow' }
        default { 'DarkGray' }
    }
    Write-Host ("[{0,-4}] {1,-46} {2}" -f $Status, $Name, $Detail) -ForegroundColor $color
    foreach ($line in $Fix) { Write-Host ("        → $line") -ForegroundColor DarkYellow }
}

function Invoke-Gate {
    <#
        $Action 返回 $null 表示成功，或返回一段说明（失败原因）。
        抛出异常也算失败。
    #>
    param(
        [string]$Gate,
        [string]$Name,
        [scriptblock]$Action,
        [string[]]$Fix = @()
    )
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $problem = & $Action
    } catch {
        $problem = $_.Exception.Message
    }
    $timer.Stop()
    $seconds = '{0:N1}s' -f $timer.Elapsed.TotalSeconds
    if ($null -eq $problem) {
        Add-Result -Gate $Gate -Name $Name -Status PASS -Detail $seconds
        return $true
    }
    Add-Result -Gate $Gate -Name $Name -Status FAIL -Detail $problem -Fix $Fix
    return $false
}

function Invoke-Native {
    <#
        跑外部命令并保留输出（不因非 0 退出码抛异常，由调用方判断）。
        返回 @{ Code = <int>; Output = <string[]> }
    #>
    param(
        [string]$File,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $RepoRoot,
        [hashtable]$Environment = @{}
    )
    $saved = @{}
    foreach ($key in $Environment.Keys) {
        $saved[$key] = [System.Environment]::GetEnvironmentVariable($key, 'Process')
        [System.Environment]::SetEnvironmentVariable($key, $Environment[$key], 'Process')
    }
    Push-Location -LiteralPath $WorkingDirectory
    $previousEap = $ErrorActionPreference
    # Windows PowerShell 5.1 会把外部命令的 stderr 当成 ErrorRecord；在 Stop 下直接抛错。
    # ffmpeg / alembic / npm 都会往 stderr 写正常日志，这里必须放开。
    $ErrorActionPreference = 'Continue'
    try {
        # StrictMode 下没跑过外部命令时 $LASTEXITCODE 不存在，先归零。
        $global:LASTEXITCODE = 0
        $output = & $File @Arguments 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = $output }
    } finally {
        $ErrorActionPreference = $previousEap
        Pop-Location
        foreach ($key in $saved.Keys) {
            [System.Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process')
        }
    }
}

# --- 开始 -------------------------------------------------------------------

Write-Host ''
Write-Host '漫剧工作台 · 干净环境复现门禁' -ForegroundColor Cyan
Write-Host "仓库根目录：$RepoRoot"
Write-Host "测试库    ：$TestDatabaseUrl$(if ($UsingDefaultTestUrl) { '（conftest 内置默认值）' })"
Write-Host ''

if ($UsingDefaultTestUrl) {
    Add-Result -Gate G6 -Name 'MANJU_TEST_DATABASE_URL' -Status WARN -Detail '未设置，用 conftest.py 的内置默认值' -Fix @(
        '本机 MySQL 账号不是 root/root 时必填：',
        '$env:MANJU_TEST_DATABASE_URL = "mysql+pymysql://<user>:<password>@127.0.0.1:3306/manju_director_test?charset=utf8mb4"'
    )
}

# G1 依赖安装 + 导入自检 -----------------------------------------------------
if ($SkipInstall) {
    Add-Result -Gate G1 -Name '后端依赖安装' -Status SKIP -Detail '按参数跳过（-SkipInstall）'
}
else {
    if (-not (Test-Path $RequirementsPath)) {
        Add-Result -Gate G1 -Name '后端依赖安装' -Status FAIL -Detail "找不到 $RequirementsPath" -Fix @(
            '锁定文件缺失：确认仓库完整 clone，或改用 -Requirements backend\requirements.txt（不锁定版本）。'
        )
    }
    else {
        [void](Invoke-Gate -Gate G1 -Name "pip install -r $([System.IO.Path]::GetFileName($RequirementsPath))" -Fix @(
                '网络/镜像问题：加 -i https://pypi.tuna.tsinghua.edu.cn/simple 重试。',
                '不要装进系统 Python：本脚本固定用 --target backend\.pylibs。'
            ) -Action {
                $result = Invoke-Native -File 'python' -Arguments @(
                    '-m', 'pip', 'install', '--target', $Pylibs, '-r', $RequirementsPath
                )
                if ($result.Code -ne 0) {
                    return "pip 退出码 $($result.Code)：$($result.Output | Select-Object -Last 1)"
                }
                return $null
            })
    }
}

[void](Invoke-Gate -Gate G1 -Name 'import fastapi, sqlmodel, alembic' -Fix @(
        '依赖没装齐：重跑本脚本（不要加 -SkipInstall）。',
        '装到了别处：确认 PYTHONPATH 含 backend\.pylibs 与 backend。'
    ) -Action {
        $result = Invoke-Native -File 'python' -Arguments @(
            '-c', 'import fastapi, sqlmodel, alembic; print(fastapi.__version__, sqlmodel.__version__, alembic.__version__)'
        ) -Environment @{ PYTHONPATH = "$Pylibs;$Backend" }
        if ($result.Code -ne 0) { return "导入失败（退出码 $($result.Code)）" }
        return $null
    })

# G2 前端依赖 ---------------------------------------------------------------
if ($SkipInstall) {
    Add-Result -Gate G2 -Name '前端依赖安装' -Status SKIP -Detail '按参数跳过（-SkipInstall）'
}
elseif (-not (Test-Path (Join-Path $Frontend 'package-lock.json'))) {
    Add-Result -Gate G2 -Name 'npm ci --prefix frontend' -Status FAIL -Detail 'frontend/package-lock.json 不存在' -Fix @(
        '没有 lockfile 无法复现依赖；这不是「先 npm install 生成一下」的场景，lockfile 必须入库。'
    )
}
else {
    [void](Invoke-Gate -Gate G2 -Name 'npm ci --prefix frontend' -Fix @(
            'npm 要走网络；离线环境请先在有网机器上备好缓存。',
            '不要换成 npm install：它不保证与 lockfile 一致。'
        ) -Action {
            $result = Invoke-Native -File 'npm' -Arguments @('ci', '--prefix', $Frontend)
            if ($result.Code -ne 0) {
                return "npm 退出码 $($result.Code)：$($result.Output | Select-Object -Last 1)"
            }
            return $null
        })
}

# G3 基础设施：版本 / ffmpeg / MySQL ----------------------------------------
$checkArgs = @((Join-Path $Backend 'scripts\check_env.py'))
if ($Lenient) { $checkArgs += '--lenient' }
[void](Invoke-Gate -Gate G3 -Name '环境预检 check_env.py' -Fix @(
        '按脚本打印的逐项提示修环境（版本 / ffmpeg / MySQL 凭证）。'
    ) -Action {
        $envVars = @{}
        if ($TestDatabaseUrl) { $envVars['MANJU_TEST_DATABASE_URL'] = $TestDatabaseUrl }
        $result = Invoke-Native -File 'python' -Arguments $checkArgs -Environment $envVars
        if ($result.Code -ne 0) {
            $fails = @($result.Output | Where-Object { "$_" -match '\[ FAIL \]' })
            $tail = @($result.Output) | Select-Object -Last 1
            return "预检未通过：$($fails -join '；') $tail"
        }
        return $null
    })

# G4/G5 配置与服务启动前置 ---------------------------------------------------
[void](Invoke-Gate -Gate G4 -Name 'backend\.env 存在' -Action {
        $envFile = Join-Path $Backend '.env'
        if (Test-Path $envFile) { return $null }
        Copy-Item (Join-Path $Backend '.env.example') $envFile
        return $null
    })

# G6 后端测试 ---------------------------------------------------------------
if ($SkipTests) {
    Add-Result -Gate G6 -Name 'python -m pytest' -Status SKIP -Detail '按参数跳过（-SkipTests）'
}
else {
    [void](Invoke-Gate -Gate G6 -Name "python -m pytest（期望 $ExpectedPassed passed）" -Fix @(
            '断言失败先看是不是环境（库不是 *_test、并发的另一个 pytest 会话、缺表）。',
            '用例数对不上：要么回归，要么文档过期——两处都要改，不能只改一处。',
            '不要额外加 -q：pytest.ini 的 addopts 已有 -q，再加一层会把结尾的「117 passed」汇总行压掉。'
        ) -Action {
            $result = Invoke-Native -File 'python' -Arguments @('-m', 'pytest') `
                -WorkingDirectory $Backend `
                -Environment @{
                    PYTHONPATH            = "$Pylibs;$Backend"
                    PYTHONUTF8            = '1'
                    MANJU_TEST_DATABASE_URL = $TestDatabaseUrl
                    APP_MODE              = 'mock'
                    WORKER_ENABLED        = 'false'
                }
            $summary = ($result.Output |
                    Where-Object { [regex]::IsMatch([string]$_, '\d+ (passed|failed|error)') } |
                    Select-Object -Last 1)
            # 注意：空管道的结果是 AutomationNull，[string] 转换后仍是 null，必须先归一成 ''。
            $summaryText = if ($null -eq $summary) { '' } else { "$summary" }
            if ($result.Code -ne 0) {
                $tail = (@($result.Output) | Select-Object -Last 5) -join ' / '
                return "pytest 退出码 $($result.Code)：$tail"
            }
            $match = [regex]::Match($summaryText, '(\d+) passed')
            if (-not $match.Success) {
                return "没解析到通过数：$summaryText"
            }
            $passed = [int]$match.Groups[1].Value
            if ($passed -ne $ExpectedPassed) {
                return "通过 $passed 项，期望 $ExpectedPassed 项（文档/代码有一方过期）"
            }
            return $null
        })
}

# G8 契约与迁移 -------------------------------------------------------------
[void](Invoke-Gate -Gate G8 -Name 'OpenAPI 契约校验' -Fix @(
        '手写契约与服务端实现不一致时先改契约再改代码（契约是设计合同）。'
    ) -Action {
        $envVars = @{ PYTHONPATH = "$Pylibs;$Backend" }
        $export = Invoke-Native -File 'python' -Arguments @((Join-Path $Backend 'scripts\export_openapi.py')) `
            -WorkingDirectory $Backend -Environment $envVars
        if ($export.Code -ne 0) { return "export_openapi.py 退出码 $($export.Code)" }
        $validate = Invoke-Native -File 'python' -Arguments @((Join-Path $Backend 'scripts\validate_openapi.py')) `
            -WorkingDirectory $Backend -Environment $envVars
        if ($validate.Code -ne 0) { return "validate_openapi.py 退出码 $($validate.Code)" }
        return $null
    })

if ($SkipAlembic) {
    Add-Result -Gate G8 -Name 'alembic check（无漂移）' -Status SKIP -Detail '按参数跳过（-SkipAlembic）'
}
else {
    [void](Invoke-Gate -Gate G8 -Name 'alembic check（无漂移）' -Fix @(
            '漂移必须对着「已迁到 head 的库」跑；对遗留演示库跑会报历史漂移，不代表仓库有问题。'
        ) -Action {
            $envVars = @{
                PYTHONPATH   = "$Pylibs;$Backend"
                DATABASE_URL = $TestDatabaseUrl
            }
            foreach ($step in @(
                    @('downgrade', 'base'),
                    @('upgrade', 'head'),
                    @('check')
                )) {
                $result = Invoke-Native -File 'python' -Arguments (@('-m', 'alembic', '-c', 'alembic.ini') + $step) `
                    -WorkingDirectory $Backend -Environment $envVars
                if ($result.Code -ne 0) {
                    return "alembic $($step -join ' ') 退出码 $($result.Code)"
                }
                if ($step[0] -eq 'check' -and (($result.Output -join "`n") -notmatch 'No new upgrade operations detected')) {
                    return 'alembic check 没有输出 No new upgrade operations detected'
                }
            }
            return $null
        })
}

# G7 前端构建 ---------------------------------------------------------------
if ($SkipFrontendBuild) {
    Add-Result -Gate G7 -Name 'npm run build --prefix frontend' -Status SKIP -Detail '按参数跳过（-SkipFrontendBuild）'
}
else {
    [void](Invoke-Gate -Gate G7 -Name 'npm run build --prefix frontend' -Fix @(
            '先确认 node_modules 存在（不加 -SkipInstall 重跑即可）。',
            '类型错误属于代码问题，按 tsc 输出逐个修。'
        ) -Action {
            $result = Invoke-Native -File 'npm' -Arguments @('run', 'build', '--prefix', $Frontend)
            if ($result.Code -ne 0) {
                $tail = ($result.Output | Select-Object -Last 3) -join ' / '
                return "npm 退出码 $($result.Code)：$tail"
            }
            return $null
        })
}

# --- 汇总 -------------------------------------------------------------------

$failed = @($script:Results | Where-Object { $_.Status -eq 'FAIL' })
$skipped = @($script:Results | Where-Object { $_.Status -eq 'SKIP' })

Write-Host ''
Write-Host '── 汇总 ───────────────────────────────────────────────' -ForegroundColor Cyan
foreach ($row in $script:Results) {
    $color = switch ($row.Status) {
        'PASS' { 'Green' }
        'FAIL' { 'Red' }
        'WARN' { 'Yellow' }
        default { 'DarkGray' }
    }
    Write-Host ("{0,-4} {1,-6} {2}" -f $row.Status, $row.Gate, $row.Name) -ForegroundColor $color
}
Write-Host ''
if ($failed.Count -gt 0) {
    Write-Host "结论：$($failed.Count) 项失败 —— 先修环境/入口，不要先怀疑业务代码。" -ForegroundColor Red
    exit 1
}
if ($skipped.Count -gt 0) {
    Write-Host "结论：通过（$($skipped.Count) 项按参数跳过，结论不完整）。" -ForegroundColor Yellow
    exit 0
}
Write-Host '结论：干净环境复现门禁全绿。' -ForegroundColor Green
exit 0
}
finally {
    foreach ($name in $script:InheritedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $script:InheritedEnvironment[$name], 'Process')
    }
}
