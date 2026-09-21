"""独立复核工具：验证 manju-director 的干净环境门禁。

这个脚本只写测试库，不修改应用库；所有子进程都使用隔离后的环境变量。
尤其会移除外部 DEBUG，避免 ``DEBUG=release`` 之类的 shell 状态覆盖 .env。

用法（仓库根目录或任意目录均可）：
    python scripts/review_clean_env.py
    python scripts/review_clean_env.py --skip-service
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
PYLIBS = BACKEND / ".pylibs"
DEFAULT_TEST_DB = (
    "mysql+pymysql://root:root@127.0.0.1:3306/manju_director_review_test?charset=utf8mb4"
)


def clean_env(test_db: str) -> dict[str, str]:
    env = os.environ.copy()
    # Pydantic Settings reads process variables before backend/.env.
    # Do not let an unrelated shell setting change the application schema.
    env.pop("DEBUG", None)
    env.pop("DATABASE_URL", None)
    env["DATABASE_URL"] = test_db
    env["MANJU_TEST_DATABASE_URL"] = test_db
    env["APP_MODE"] = "mock"
    env["WORKER_ENABLED"] = "false"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.pathsep.join((str(PYLIBS), str(BACKEND)))
    return env


def run(name: str, command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300) -> bool:
    print(f"\n[{name}] {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}")
        return False
    output = (result.stdout + result.stderr).strip()
    if output:
        print(output[-5000:])
    if result.returncode:
        print(f"FAIL: exit code {result.returncode}")
        return False
    print("PASS")
    return True


def executable_version(name: str, args: list[str], expected: str | None = None) -> bool:
    exe = shutil.which(name) or shutil.which(f"{name}.exe")
    if not exe:
        print(f"[{name}] FAIL: executable not found")
        return False
    result = subprocess.run([exe, *args], capture_output=True, text=True, errors="replace")
    output = (result.stdout + result.stderr).strip()
    first = output.splitlines()[0] if output else ""
    print(f"[{name}] {first}")
    if result.returncode:
        print(f"FAIL: exit code {result.returncode}")
        return False
    if expected and not re.search(expected, first):
        print(f"FAIL: expected pattern {expected!r}")
        return False
    print("PASS")
    return True


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def service_smoke(env: dict[str, str], port: int) -> bool:
    if not port_free(port):
        print(f"[service] FAIL: 127.0.0.1:{port} is already occupied")
        return False
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    print(f"\n[service] {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + 45
        last_lines: list[str] = []
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    print(f"health={response.status} {body}")
                    if response.status != 200:
                        return False
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=2) as docs:
                        print(f"docs={docs.status}")
                    print("PASS")
                    return True
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(0.5)
        if process.stdout:
            last_lines = process.stdout.read().splitlines()[-20:]
        print("FAIL: service did not become healthy")
        if last_lines:
            print("\n".join(last_lines))
        return False
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    # Windows PowerShell 5.1 常见的 GBK stdout 不能打印 npm 的 ✓ 等字符。
    # 复核结果必须能完整输出，不能因日志编码让门禁误报。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="manju-director 干净环境独立复核")
    parser.add_argument("--test-db", default=os.environ.get("MANJU_TEST_DATABASE_URL", DEFAULT_TEST_DB))
    parser.add_argument("--skip-service", action="store_true")
    args = parser.parse_args()
    if not re.search(r"[^/]+_test(?:\?.*)?$", args.test_db.split("/")[-1]):
        print("FAIL: --test-db 的数据库名必须以 _test 结尾")
        return 2

    env = clean_env(args.test_db)
    results: list[bool] = []
    print(f"repo={ROOT}")
    print(f"test_db={args.test_db}")
    print(f"isolated_DEBUG={'DEBUG' not in env}")
    results.append(executable_version("python", ["--version"], r"Python 3\.13\."))
    results.append(executable_version("node", ["--version"], r"v24\."))
    results.append(executable_version("ffmpeg", ["-version"]))
    results.append(executable_version("ffprobe", ["-version"]))
    results.append(run("imports", [sys.executable, "-c", "import fastapi, sqlmodel, alembic; print('imports ok')"], cwd=ROOT, env=env, timeout=60))
    results.append(run("pytest", [sys.executable, "-m", "pytest"], cwd=BACKEND, env=env, timeout=900))
    results.append(run("openapi export", [sys.executable, "scripts/export_openapi.py"], cwd=BACKEND, env=env, timeout=120))
    results.append(run("openapi validate", [sys.executable, "scripts/validate_openapi.py"], cwd=BACKEND, env=env, timeout=120))
    for operation in (("alembic downgrade", "downgrade", "base"), ("alembic upgrade", "upgrade", "head"), ("alembic check", "check")):
        results.append(run(operation[0], [sys.executable, "-m", "alembic", "-c", "alembic.ini", *operation[1:]], cwd=BACKEND, env=env, timeout=240))
    npm = shutil.which("npm.cmd") or shutil.which("npm") or "npm.cmd"
    results.append(run("frontend build", [npm, "run", "build", "--prefix", str(FRONTEND)], cwd=ROOT, env=env, timeout=240))
    if not args.skip_service:
        results.append(service_smoke(env, 8011))
    failed = sum(not item for item in results)
    print(f"\nRESULT: {'PASS' if not failed else 'FAIL'} ({len(results) - failed}/{len(results)} checks passed)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
