@echo off
REM 统一入口：把工作区内的依赖目录挂到 PYTHONPATH，再调用系统 Python。
REM 用法：run.cmd -m uvicorn app.main:app --port 8010
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%.pylibs;%HERE%"
set "PIP_CACHE_DIR=%HERE%..\.cache\pip"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cd /d "%HERE%"
python %*
endlocal
