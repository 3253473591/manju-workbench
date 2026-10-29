#!/bin/sh
set -e

# 迁移必须在这里跑：`03` §12.1 要求启动即校验库已迁移，否则进程直接非零退出。
echo "[entrypoint] alembic upgrade head"
alembic upgrade head

echo "[entrypoint] 启动 uvicorn"
exec uvicorn manju.main:app --host 0.0.0.0 --port 8000
