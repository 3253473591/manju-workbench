"""配置项审计：找出"写了但没人读"和"读了但没写进示例"的配置。

为什么需要它：wind-comic 的 v13 发布说明里，最大一类事故是
"**设置看起来生效了，但没有任何代码读它**"（纵横比、色彩空间、安全区各自读了不同来源）。
这类问题不会报错、不会失败测试，只会在某条路径上悄悄用默认值。

本脚本做三件事：
1. `.env.example` 里的每个键，是否在仓库任何位置被引用（严格模式下未引用即失败）；
2. `config.py` 里 `os.environ.get("X")` 读的键，是否写进了 `.env.example`；
3. `Settings` 的字段，是否在 `config.py` 之外被读取（只警告 —— 有些字段是对外契约）。

用法：
    python scripts/settings_audit.py            # 报告
    python scripts/settings_audit.py --strict   # 有"死键"或"缺示例"时退出码 1（给 CI 用）
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "apps" / "api"
CONFIG = API / "manju" / "config.py"
ENV_EXAMPLE = ROOT / ".env.example"

SKIP_DIRS = {
    ".git", "node_modules", "reference", "dist", ".pytest_cache", "__pycache__",
    ".tmp-chrome", ".venv", "venv", "docs/我师父是个普通人", "docs/提示词", "playwright-report",
    "test-results",
}
SCAN_SUFFIXES = {".py", ".ts", ".tsx", ".yml", ".yaml", ".json", ".md", ".ps1", ".sh", ".ini", ".toml", ".example"}


@dataclass
class Finding:
    key: str
    kind: str  # dead | missing-example | unread-field
    detail: str


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if any(part in relative for part in SKIP_DIRS):
            continue
        files.append(path)
    return files


def _env_keys() -> list[str]:
    keys: list[str] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
        if match:
            keys.append(match.group(1))
    return keys


def _config_env_keys() -> list[str]:
    source = CONFIG.read_text(encoding="utf-8")
    return sorted(set(re.findall(r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]*)"', source)))


def _settings_fields() -> list[str]:
    tree = ast.parse(CONFIG.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return [item.target.id for item in node.body if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)]
    raise SystemExit("config.py 里找不到 Settings 类")


def audit(strict: bool) -> int:
    files = _iter_files()
    texts = {path: path.read_text(encoding="utf-8", errors="replace") for path in files}
    findings: list[Finding] = []

    env_keys = _env_keys()
    for key in env_keys:
        users = [path.relative_to(ROOT).as_posix() for path, text in texts.items() if key in text and path != ENV_EXAMPLE]
        if not users:
            findings.append(Finding(key, "dead", "只在 .env.example 出现，没有任何代码读它"))

    example_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in _config_env_keys():
        if key not in example_text:
            findings.append(Finding(key, "missing-example", "config.py 读取，但没写进 .env.example"))

    fields = _settings_fields()
    api_texts = {
        path: text for path, text in texts.items()
        if path.suffix == ".py" and API in path.parents and path != CONFIG
    }
    for field in fields:
        pattern = re.compile(rf"settings\.{re.escape(field)}\b")
        users = [path.relative_to(ROOT).as_posix() for path, text in api_texts.items() if pattern.search(text)]
        if not users:
            findings.append(Finding(field, "unread-field", "Settings 字段在 config.py 之外没有任何 settings.<field> 读取"))

    print(f"扫描 {len(files)} 个文件；.env.example {len(env_keys)} 个键；Settings {len(fields)} 个字段")
    print()
    if not findings:
        print("✅ 没有发现死键、缺示例或未读字段")
        return 0

    for kind, label in (("dead", "死键（写了没人读）"), ("missing-example", "缺示例（读了没写）"), ("unread-field", "未读字段（警告）")):
        group = [item for item in findings if item.kind == kind]
        if not group:
            continue
        print(f"{label}: {len(group)}")
        for item in group:
            print(f"  - {item.key}: {item.detail}")
        print()

    hard = [item for item in findings if item.kind in ("dead", "missing-example")]
    if strict and hard:
        print(f"❌ 严格模式：{len(hard)} 个阻断项（死键或缺少示例）")
        return 1
    print(f"报告完成（{len(hard)} 个需要处理，{len(findings) - len(hard)} 个警告）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    return audit(args.strict)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
