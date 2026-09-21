"""启动前环境预检：把「版本不对 / 缺依赖 / 缺 ffmpeg / 数据库连不上」和「代码有问题」区分开。

只用标准库，因此**在依赖还没装的时候也能跑**：

    python backend\\scripts\\check_env.py

它检查（README §2/§3 的门禁 G1、G3、G5 对应的可执行版本）：

1. Python 版本（本仓库验证版本 3.13.x）
2. Node / npm 版本（验证版本 24.x）与 `frontend/node_modules`
3. ffmpeg / ffprobe（mock 视频链路也要它们真的编码 MP4 与抽首帧）
4. 后端依赖能否 `import fastapi, sqlmodel, alembic`（工作区 `backend/.pylibs`）
5. MySQL 连接：应用库（`backend/.env` 的 `DATABASE_URL`）
6. MySQL 连接：测试库（`MANJU_TEST_DATABASE_URL`，库名必须以 `_test` 结尾）
7. 端口占用情况（8010 / 5173 是契约默认值，8020 / 5180 是开发用）

退出码：任一 `FAIL` → 1；只有 `WARN` 或全绿 → 0。
任何一项失败都会直接打印「怎么修」，不需要去猜是环境问题还是代码问题。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent

#: 依赖装在工作区 `backend/.pylibs`；加进 sys.path 才能在没设 PYTHONPATH 时连上 MySQL 做真实校验。
_PYLIBS = BACKEND_DIR / ".pylibs"
if _PYLIBS.is_dir() and str(_PYLIBS) not in sys.path:
    sys.path.insert(0, str(_PYLIBS))

#: 本仓库在 2026-09-21 复核时验证通过的版本（见 README §2）。
VERIFIED_PYTHON = (3, 13)
VERIFIED_NODE_MAJOR = 24
VERIFIED_MYSQL_MAJOR = 8

DEFAULT_TEST_DATABASE_URL = (
    "mysql+pymysql://root:root@127.0.0.1:3306/manju_director_test?charset=utf8mb4"
)
DEFAULT_DATABASE_URL = (
    "mysql+pymysql://root:root@127.0.0.1:3306/manju_director?charset=utf8mb4"
)
CONTRACT_PORTS = (8010, 5173)
DEV_PORTS = (8020, 5180)

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

MARKERS = {OK: "  OK  ", WARN: " WARN ", FAIL: " FAIL ", SKIP: " SKIP "}


@dataclass
class Result:
    name: str
    status: str
    detail: str
    hint: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, hint: str = "") -> None:
        self.results.append(Result(name, status, detail, hint))

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if r.status == FAIL]

    @property
    def warned(self) -> list[Result]:
        return [r for r in self.results if r.status == WARN]


# --- 工具 -----------------------------------------------------------------


def run(
    cmd: list[str], timeout: float = 30.0, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """跑一条命令并拿到 stdout+stderr；找不到可执行文件返回 (-1, "")。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return -1, ""
    except subprocess.TimeoutExpired:
        return -2, f"{cmd[0]} 超过 {timeout}s 没有返回"
    except OSError as exc:  # pragma: no cover - 平台相关
        return -3, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def ffmpeg_version_token(line: str) -> str:
    """从 `ffmpeg version <token> Copyright ...` 里取出可读的版本号。

    发行版是 `7.1` / `6.1.1-full_build`；Gyan 的 git 快照是
    `N-120706-g0469d68acb-20250812`，直接打太长，收敛成 `snapshot-20250812`。
    """
    match = re.search(r"version\s+(\S+)", line)
    if not match:
        return "?"
    token = match.group(1).rstrip(",")
    snapshot = re.match(r"^N-\d+-g[0-9a-f]+-(\d{8})$", token)
    if snapshot:
        return f"snapshot-{snapshot.group(1)}"
    return token


def first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def which(name: str) -> str:
    return shutil.which(name) or ""


def read_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：只认 `KEY=VALUE`，忽略注释与空行；不做变量展开。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_URL_RE = re.compile(
    r"^(?P<driver>[a-z0-9_+]+)://(?P<user>[^:@/]*)(?::(?P<password>[^@]*))?"
    r"@(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<database>[^?]*)(?:\?(?P<query>.*))?$",
    re.IGNORECASE,
)


@dataclass
class DbTarget:
    raw: str
    driver: str
    user: str
    password: str
    host: str
    port: int
    database: str


def parse_db_url(url: str) -> DbTarget | None:
    match = _URL_RE.match(url.strip())
    if not match:
        return None
    groups = match.groupdict()
    return DbTarget(
        raw=url.strip(),
        driver=groups["driver"] or "",
        user=groups["user"] or "",
        password=groups["password"] or "",
        host=groups["host"] or "127.0.0.1",
        port=int(groups["port"] or 3306),
        database=groups["database"] or "",
    )


def port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


# --- 各项检查 -------------------------------------------------------------


def check_python(report: Report, lenient: bool) -> None:
    current = sys.version_info[:3]
    raw = f"{current[0]}.{current[1]}.{current[2]}"
    expected = f"{VERIFIED_PYTHON[0]}.{VERIFIED_PYTHON[1]}"
    if current[:2] == VERIFIED_PYTHON:
        report.add("Python 版本", OK, f"{raw}（验证版本 {expected}.x）")
    elif current[:2] > VERIFIED_PYTHON:
        report.add(
            "Python 版本",
            WARN if lenient else FAIL,
            f"{raw} 高于验证版本 {expected}.x",
            f"本仓库只在 Python {expected} 上验证过。新版本可能有依赖不兼容；"
            f"确认无误可以用 --lenient 跳过这条。",
        )
    else:
        report.add(
            "Python 版本",
            WARN if lenient else FAIL,
            f"{raw} 低于验证版本 {expected}.x",
            f"请安装 Python {expected} 后重跑；否则报错会混着版本问题，难判断是代码还是环境。",
        )


def check_node(report: Report, lenient: bool) -> None:
    exe = which("node")
    if not exe:
        report.add(
            "Node / npm",
            FAIL,
            "找不到 node",
            "安装 Node 24（推荐 LTS 安装包或 nvm-windows），然后重开终端让 PATH 生效。",
        )
        return
    code, out = run([exe, "--version"])
    version = first_line(out) if code == 0 else ""
    match = re.match(r"^v?(\d+)\.", version)
    if not match:
        report.add("Node / npm", FAIL, f"`node --version` 输出异常：{out!r}", "重装 Node。")
        return
    major = int(match.group(1))
    npm = which("npm.cmd") or which("npm")
    npm_version = first_line(run([npm, "--version"])[1]) if npm else ""
    detail = f"{version}，npm {npm_version or '缺失'}"
    if not npm:
        report.add("Node / npm", FAIL, detail, "Node 安装不完整（缺 npm），重装 Node。")
        return
    if major == VERIFIED_NODE_MAJOR:
        report.add("Node / npm", OK, detail)
    else:
        report.add(
            "Node / npm",
            WARN if lenient else FAIL,
            f"{detail}（验证版本 {VERIFIED_NODE_MAJOR}.x）",
            f"本仓库只在 Node {VERIFIED_NODE_MAJOR} 上验证过前端构建；"
            f"换版本请自己承担，或装 Node {VERIFIED_NODE_MAJOR}。",
        )


def check_ffmpeg(report: Report) -> None:
    versions: list[str] = []
    missing: list[str] = []
    for name in ("ffmpeg", "ffprobe"):
        exe = which(name) or which(f"{name}.exe")
        if not exe:
            missing.append(name)
            continue
        code, out = run([exe, "-version"])
        if code != 0:
            missing.append(f"{name}(执行失败)")
            continue
        versions.append(f"{name} {ffmpeg_version_token(first_line(out))}")
    if missing:
        report.add(
            "ffmpeg / ffprobe",
            FAIL,
            "缺失：" + "、".join(missing),
            "mock 模式也要用它们真的编码 MP4、抽首帧，缺了视频链路必失败。\n"
            "        Windows 安装：winget install Gyan.FFmpeg（或 choco install ffmpeg），\n"
            "        也可以下静态包解开后把 bin 目录加进 PATH，然后重开终端。\n"
            "        自检：ffmpeg -version 与 ffprobe -version 都能打印版本行。",
        )
    else:
        report.add("ffmpeg / ffprobe", OK, "、".join(versions))


def check_backend_deps(report: Report) -> None:
    pylibs = BACKEND_DIR / ".pylibs"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(pylibs), str(BACKEND_DIR)])
    code, out = run(
        [
            sys.executable,
            "-c",
            "import fastapi, sqlmodel, alembic;"
            "print('fastapi', fastapi.__version__, '| sqlmodel', sqlmodel.__version__,"
            "' | alembic', alembic.__version__)",
        ],
        timeout=120,
        env=env,
    )
    if code == 0:
        report.add("后端依赖 (import fastapi, sqlmodel, alembic)", OK, out)
        return
    report.add(
        "后端依赖 (import fastapi, sqlmodel, alembic)",
        FAIL,
        f"导入失败：{out.splitlines()[-1] if out else '无输出'}",
        "按锁定版本装（从仓库根目录执行）：\n"
        "        python -m pip install --target backend\\.pylibs -r backend\\requirements.lock\n"
        "        运行时要带 PYTHONPATH：$env:PYTHONPATH=\"$PWD\\backend\\.pylibs;$PWD\\backend\"",
    )


def check_mysql(
    report: Report, label: str, url: str, *, must_end_with_test: bool, note: str = ""
) -> None:
    target = parse_db_url(url)
    if target is None:
        report.add(label, FAIL, f"连接串解析失败：{url!r}", "检查格式是否形如 "
                  "mysql+pymysql://user:password@127.0.0.1:3306/dbname?charset=utf8mb4")
        return
    if must_end_with_test and not target.database.endswith("_test"):
        report.add(
            label,
            FAIL,
            f"库名 {target.database!r} 不以 _test 结尾",
            "测试会 drop / TRUNCATE 目标库。请在 MANJU_TEST_DATABASE_URL 里指向 *_test 库，"
            "绝对不要指向演示库。",
        )
        return
    if not port_in_use(target.host, target.port):
        report.add(
            label,
            FAIL,
            f"连不上 {target.host}:{target.port}（端口未监听）",
            "MySQL 8 没起来或端口不对：确认服务已启动，"
            "并用 `mysql -h 127.0.0.1 -P 3306 -u <user> -p` 手工验证一次。",
        )
        return
    try:
        import pymysql  # type: ignore[import-not-found]
    except ImportError:
        report.add(
            label,
            WARN,
            f"{target.host}:{target.port} 有监听，但没装 PyMySQL，无法进一步验证账号与库",
            "先装后端依赖，再重跑本脚本。",
        )
        return

    driver = "pymysql" if "pymysql" in target.driver else target.driver
    if driver != "pymysql":  # pragma: no cover - 只支持 MySQL
        report.add(label, WARN, f"暂不支持的驱动 {driver!r}", "")
        return
    try:
        connection = pymysql.connect(
            host=target.host,
            port=target.port,
            user=target.user,
            password=target.password,
            connect_timeout=5,
            read_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - 要把原始错误原样给用户看
        report.add(
            label,
            FAIL,
            f"{target.host}:{target.port} 在监听，但账号 {target.user!r} 连接失败：{exc}",
            "这是凭证问题，不是代码问题。设置完整连接串后再跑：\n"
            '        $env:MANJU_TEST_DATABASE_URL = '
            '"mysql+pymysql://<user>:<password>@127.0.0.1:3306/manju_director_test?charset=utf8mb4"\n'
            "        该账号需要能 CREATE DATABASE、跑 Alembic 迁移、TRUNCATE 测试表。",
        )
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            server = str(cursor.fetchone()[0])
            cursor.execute("SHOW DATABASES LIKE %s", (target.database,))
            exists = cursor.fetchone() is not None
    finally:
        connection.close()

    server_major = server.split(".")[0]
    extra = f"；{note}" if note else ""
    if server_major != str(VERIFIED_MYSQL_MAJOR):
        report.add(
            label,
            WARN,
            f"MySQL {server}（验证版本 {VERIFIED_MYSQL_MAJOR}.x），库 "
            f"{target.database}{'已存在' if exists else '尚未创建'}{extra}",
            f"本仓库只在 MySQL {VERIFIED_MYSQL_MAJOR} 上验证过迁移与 GET_LOCK 串行化。",
        )
    elif exists:
        report.add(
            label,
            OK,
            f"MySQL {server}，{target.user}@{target.host}:{target.port}，"
            f"库 {target.database} 已存在{extra}",
        )
    else:
        report.add(
            label,
            OK,
            f"MySQL {server}，{target.user}@{target.host}:{target.port} 可连接；"
            f"库 {target.database} 还不存在，启动服务/跑测试时会自动创建{extra}",
        )


def check_frontend(report: Report) -> None:
    lock = PROJECT_DIR / "frontend" / "package-lock.json"
    modules = PROJECT_DIR / "frontend" / "node_modules"
    if not lock.is_file():
        report.add(
            "前端依赖锁",
            FAIL,
            "frontend/package-lock.json 不存在",
            "没有 lockfile 就无法复现依赖；`npm ci` 会直接失败。",
        )
        return
    detail = "package-lock.json 存在"
    if modules.is_dir():
        detail += "；node_modules 已存在"
        report.add("前端依赖锁", OK, detail)
    else:
        report.add(
            "前端依赖锁",
            WARN,
            detail + "；node_modules 尚未安装",
            "从仓库根目录执行：npm ci --prefix frontend（不要用 npm install，它不保证 lock 一致）。",
        )


def check_ports(report: Report) -> None:
    busy_contract = [p for p in CONTRACT_PORTS if port_in_use("127.0.0.1", p)]
    busy_dev = [p for p in DEV_PORTS if port_in_use("127.0.0.1", p)]
    if not busy_contract and not busy_dev:
        report.add("端口", OK, "8010 / 5173 / 8020 / 5180 都空闲")
        return
    parts = []
    if busy_contract:
        parts.append("契约默认端口被占用：" + "、".join(map(str, busy_contract)))
    if busy_dev:
        parts.append("开发端口已在使用：" + "、".join(map(str, busy_dev)))
    report.add(
        "端口",
        WARN,
        "；".join(parts),
        "端口被占用不是错误（可能就是你自己的服务）。要换端口：\n"
        "        后端 --port 8020；前端 $env:MANJU_BACKEND / $env:MANJU_WEB_PORT，两边要一起改。",
    )


def check_test_db_url_provided(report: Report) -> tuple[str, str]:
    explicit = os.environ.get("MANJU_TEST_DATABASE_URL", "").strip()
    if explicit:
        report.add(
            "MANJU_TEST_DATABASE_URL",
            OK,
            "已显式设置（测试不会读 DATABASE_URL）",
        )
        return explicit, "来源：环境变量 MANJU_TEST_DATABASE_URL"
    report.add(
        "MANJU_TEST_DATABASE_URL",
        WARN,
        f"未设置，将回退到 conftest.py 的内置默认值 {DEFAULT_TEST_DATABASE_URL}",
        "本机 MySQL 账号不是 root/root 时**必须**显式设置（见 README §3.1「G6 后端测试」那一段）：\n"
        '        $env:MANJU_TEST_DATABASE_URL = '
        '"mysql+pymysql://<user>:<password>@127.0.0.1:3306/manju_director_test?charset=utf8mb4"',
    )
    return DEFAULT_TEST_DATABASE_URL, "来源：conftest.py 内置默认值"


# --- 入口 -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="启动前环境预检（只用标准库，依赖没装也能跑）"
    )
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="版本不完全匹配时只警告不失败（默认严格匹配验证版本）",
    )
    parser.add_argument("--skip-db", action="store_true", help="跳过 MySQL 检查")
    parser.add_argument("--skip-ports", action="store_true", help="跳过端口检查")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    report = Report()
    env_file = BACKEND_DIR / ".env"
    env_values = read_env_file(env_file)
    app_url = (
        os.environ.get("DATABASE_URL", "").strip()
        or env_values.get("DATABASE_URL", "").strip()
        or DEFAULT_DATABASE_URL
    )
    app_url_source = (
        "环境变量 DATABASE_URL"
        if os.environ.get("DATABASE_URL", "").strip()
        else (f"{env_file}" if env_values.get("DATABASE_URL") else "内置默认值")
    )

    check_python(report, args.lenient)
    check_node(report, args.lenient)
    check_ffmpeg(report)
    check_backend_deps(report)
    check_frontend(report)
    test_url, test_url_source = check_test_db_url_provided(report)
    if not args.skip_db:
        check_mysql(
            report,
            "MySQL · 应用库",
            app_url,
            must_end_with_test=False,
            note=f"来源：{app_url_source}",
        )
        check_mysql(
            report,
            "MySQL · 测试库",
            test_url,
            must_end_with_test=True,
            note=test_url_source,
        )
    if not args.skip_ports:
        check_ports(report)

    if args.json:
        payload = {
            "projectDir": str(PROJECT_DIR),
            "python": ".".join(map(str, sys.version_info[:3])),
            "results": [asdict(r) for r in report.results],
            "failed": len(report.failed),
            "warned": len(report.warned),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("漫剧工作台 · 环境预检")
        print(f"仓库根目录：{PROJECT_DIR}")
        print("")
        for result in report.results:
            print(f"[{MARKERS[result.status]}] {result.name}")
            print(f"          {result.detail}")
            if result.hint and result.status in (FAIL, WARN):
                for i, line in enumerate(result.hint.splitlines()):
                    print(f"          {'提示：' if i == 0 else '      '}{line}")
        print("")
        if report.failed:
            print(
                f"结论：{len(report.failed)} 项失败，{len(report.warned)} 项警告 —— "
                "先按上面的提示修环境，再谈代码问题。"
            )
        elif report.warned:
            print(f"结论：全部关键项通过，{len(report.warned)} 项警告（不影响启动）。")
        else:
            print("结论：全部通过，可以启动服务 / 跑测试。")

    return 1 if report.failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 老终端
            pass
    raise SystemExit(main())
