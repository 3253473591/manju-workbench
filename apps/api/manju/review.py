"""动线质检与验收判定（`docs/video-motion-review/SKILL.md`，PRD P0-6）。

分三层，判决权依次往内容层移交：
- `scan()` 调外部 `vscan.ps1` 拿动线指标 —— **运动是分诊信号，不是判决**；
- `judge()` 把能靠动线判的验收点判成 pass / fail，判不了的写 **unknown**；
- `judge_with_vision()` 把 remaining unknown 交给视觉模型（抽 6 帧拼成联络图再问），
  仍判不了就继续保持 unknown。判不了 ≠ 通过，这条不能松。

降级是硬要求：脚本、ffmpeg、ffprobe 任一不在就 `degraded=True` 返回 unknown，
让链路继续跑（开源仓库不能要求别人先装仓库外的脚本）。路径用 `VSCAN_PATH` 覆写。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

from manju.providers import Provider, ProviderError

ROOT = Path(__file__).resolve().parents[2]
SCAN_TIMEOUT = 120

# 联络图：一帧一镜，3×2 刚好覆盖 6 s 的片段；再长就按时间均分
VISION_FRAMES = 6
FRAME_WIDTH = 480
FFPROBE_TIMEOUT = 15
FFMPEG_TIMEOUT = 60

RE_SAMPLES = re.compile(r"scan\s*:\s*(\d+)\s*samples")
RE_CLIP = re.compile(r"clip\s*:\s*([\d.]+)\s*s")
RE_STATS = re.compile(r"stats\s*:\s*min=([\d.]+)\s+avg=([\d.]+)\s+max=([\d.]+)(?:\s+sd=([\d.]+))?")
RE_FLAG = re.compile(r"t=\s*([\d.]+)s\s+motion=\s*([\d.]+)\s+\[([^\]]+)\]")
RE_SAMPLE = re.compile(r"^\s*([\d.]+)s\s+\|.*\|\s+([\d.]+)\s*$", re.MULTILINE)

# 验收点 → 动线能判什么。只认这几类关键词，其余一律 unknown（内容级判据要视觉模型）
MOTION_RULES = {
    "opening": ("开场", "空景", "定镜"),
    "freeze": ("定格", "静止", "静帧", "冻结", "停住"),
    "motion": ("卡顿", "抖动", "不够动", "动起来", "急推", "推进", "镜头运动"),
}


@dataclass(frozen=True)
class Motion:
    samples: int
    duration_s: Decimal | None
    avg: float
    peak: float
    minimum: float
    freeze: int
    tail_collapse: bool
    flagged: list[dict] = field(default_factory=list)
    series: list[float] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "samples": self.samples,
            "duration_s": str(self.duration_s) if self.duration_s is not None else None,
            "avg": self.avg,
            "max": self.peak,
            "min": self.minimum,
            "freeze": self.freeze,
            "tail_collapse": self.tail_collapse,
            "flagged": self.flagged,
        }


@dataclass(frozen=True)
class Report:
    motion: Motion | None
    results: list[dict]
    verdict: str  # pass | fail | unknown
    degraded: bool
    note: str | None = None


def vscan_path() -> Path | None:
    """`VSCAN_PATH` 优先；否则只找仓库内的 `tools/vscan.ps1` —— 不硬编码仓库外路径。"""
    import os

    override = os.environ.get("VSCAN_PATH")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    candidate = ROOT / "tools" / "vscan.ps1"
    return candidate if candidate.is_file() else None


def _host() -> str | None:
    """找 PowerShell 宿主。`pwsh`（7+）优先，退到 `powershell`（5.1）——
    实测这台机器只有 5.1，`vscan.ps1` 在 5.1 上能跑。"""
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _quote(value: object) -> str:
    """PowerShell 单引号字面量。路径里带中文和空格是常态，必须转义而不是拼接裸串。"""
    return "'" + str(value).replace("'", "''") + "'"


def scan(media_path: Path) -> tuple[Motion | None, str | None]:
    """调 `vscan.ps1 -ScanOnly`。返回 `(动线, 失败原因)`，两者必有一个为 None。

    用 `-Command` 包一层而不是 `-File`：PowerShell 5.1 往管道输出时默认用 OEM 代码页，
    中文路径会解码失败；开头把 `[Console]::OutputEncoding` 设成 UTF-8 才能稳定取到输出。
    """
    script = vscan_path()
    if script is None:
        return None, "未找到 vscan.ps1（设 VSCAN_PATH 或把脚本放到 tools/）"
    if not media_path.is_file():
        return None, f"产物不存在：{media_path.name}"
    host = _host()
    if host is None:
        return None, "未找到 PowerShell 宿主（pwsh / powershell 都不在 PATH）"
    command = (
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        f"& {_quote(script)} -Path {_quote(media_path)} -ScanOnly"
    )
    try:
        # `-ExecutionPolicy Bypass`：脚本是从仓库外指过来的（VSCAN_PATH），常见带
        # Mark-of-the-Web 的 .ps1 在 RemoteSigned 下会直接 SecurityError 拒载。
        result = subprocess.run(
            [host, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCAN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, f"vscan 超时（>{SCAN_TIMEOUT}s）"
    except (OSError, subprocess.CalledProcessError) as exc:
        # 带上子进程 stderr 的第一行：不然只剩一个 `CalledProcessError`，排查得从头翻起
        stderr = getattr(exc, "stderr", "") or ""
        first = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
        reason = f"（{type(exc).__name__}）：{first[:120]}" if first else f"：{type(exc).__name__}"
        return None, f"vscan 执行失败{reason}"

    output = result.stdout or ""
    return parse(output)


def parse(output: str) -> tuple[Motion | None, str | None]:
    """把 vscan 的 stdout 解析成动线。单独拆出来，单测才不用真跑 PowerShell。"""
    stats = RE_STATS.search(output)
    if stats is None:
        return None, "vscan 输出里没有 stats 行"
    samples = RE_SAMPLES.search(output)
    clip = RE_CLIP.search(output)
    flagged = [
        {"t": float(match.group(1)), "motion": float(match.group(2)), "flags": match.group(3).split(",")}
        for match in RE_FLAG.finditer(output)
    ]
    series = [float(match.group(2)) for match in RE_SAMPLE.finditer(output)]
    freeze = sum(1 for item in flagged if "FREEZE" in item["flags"])
    return (
        Motion(
            samples=int(samples.group(1)) if samples else len(series),
            duration_s=Decimal(clip.group(1)) if clip else None,
            avg=float(stats.group(2)),
            peak=float(stats.group(3)),
            minimum=float(stats.group(1)),
            freeze=freeze,
            tail_collapse=any("TAIL-COLLAPSE" in item["flags"] for item in flagged),
            flagged=flagged,
            series=series,
        ),
        None,
    )


def _judge_one(criterion: str, motion: Motion) -> tuple[str, str]:
    """返回 `(result, evidence)`；`unknown` 表示这条判据动线判不了。"""
    for kind, words in MOTION_RULES.items():
        if not any(word in criterion for word in words):
            continue
        if kind == "opening":
            head = motion.series[:3]
            if not head:
                return "unknown", "缺时间轴，判不了开场是否在动"
            if max(head) < 1.0:
                return "fail", f"开场 3 个采样点动线都低于 1.0（{head}），像是定镜"
            return "pass", f"开场动线 {max(head)}"
        if kind == "freeze":
            if motion.freeze > 0 or motion.tail_collapse:
                return "fail", f"冻结 {motion.freeze} 处，末段塌陷={motion.tail_collapse}"
            return "pass", f"无冻结（{motion.samples} 采样点）"
        if kind == "motion":
            if motion.samples >= 3 and motion.avg < 1.5:
                return "fail", f"平均动线 {motion.avg} 过低，画面基本不动"
            return "pass", f"avg={motion.avg} max={motion.peak}"
    return "unknown", "内容级判据，动线判不了（需视觉模型）"


def _verdict(results: list[dict]) -> str:
    """任一 fail 即 fail；有判过的就是 pass；一条都没判出来还是 unknown（`03` §6.3）。"""
    judged = [item for item in results if item["result"] != "unknown"]
    if any(item["result"] == "fail" for item in judged):
        return "fail"
    if judged:
        return "pass"
    return "unknown"


def judge(acceptance: list[dict] | None, motion: Motion | None, note: str | None) -> Report:
    """脏数据防线：验收点是 JSON 列，历史行可能缺 `id` / `criterion`（`01` §3）。
    缺判据的只能记 `unknown`，不许 `KeyError` —— 那会把一次质检变成 500。"""
    if motion is None:
        results = [
            {"id": item.get("id"), "result": "unknown", "evidence": note or "降级"} for item in acceptance or []
        ]
        return Report(motion=None, results=results, verdict="unknown", degraded=True, note=note)
    results = []
    for item in acceptance or []:
        criterion = str(item.get("criterion") or "")
        if not criterion:
            results.append({"id": item.get("id"), "result": "unknown", "evidence": "验收点缺 criterion（脏数据）"})
            continue
        verdict, evidence = _judge_one(criterion, motion)
        results.append({"id": item.get("id"), "result": verdict, "evidence": evidence})
    return Report(motion=motion, results=results, verdict=_verdict(results), degraded=False, note=note)


def probe_duration(media_path: Path) -> float | None:
    """实际时长。联络图要按它均分采样点，读不到就不猜。"""
    exe = shutil.which("ffprobe")
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(media_path)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFPROBE_TIMEOUT,
        )
        return float(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def contact_sheet(media_path: Path, out_dir: Path) -> tuple[Path | None, str | None]:
    """抽 `VISION_FRAMES` 帧拼成一张 3×2 联络图。返回 `(图, 失败原因)`。

    视觉模型一次能看一张图，看不了视频流；把整段压成一张比逐帧问 6 次便宜、也比只抽首帧诚实
    （首帧过审、末帧塌陷是这类片子的典型故障）。降级返回原因，绝不让质检阻断生成链路。
    """
    exe = shutil.which("ffmpeg")
    if exe is None:
        return None, "未找到 ffmpeg，跳过视觉质检"
    duration = probe_duration(media_path)
    if duration is None or duration <= 0:
        return None, "ffprobe 读不到时长，跳过视觉质检"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{media_path.name}:{media_path.stat().st_size}:{media_path.stat().st_mtime_ns}"
    sheet = out_dir / f"sheet-{hashlib.sha1(stamp.encode('utf-8')).hexdigest()[:12]}.jpg"
    fps = VISION_FRAMES / duration
    columns = 3
    command = [
        exe, "-y", "-loglevel", "error", "-i", str(media_path),
        "-vf", f"fps={fps:.6f},scale={FRAME_WIDTH}:-2,tile={columns}x{VISION_FRAMES // columns}",
        "-frames:v", "1", str(sheet),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"抽帧超时（>{FFMPEG_TIMEOUT}s）"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"抽帧失败：{type(exc).__name__}"
    if not sheet.is_file() or sheet.stat().st_size == 0:
        return None, "抽帧没有产出图"
    return sheet, None


def judge_with_vision(
    acceptance: list[dict] | None,
    report: Report,
    media_path: Path,
    provider: Provider,
    *,
    workdir: Path,
) -> Report:
    """把动线判不了的那几条交给视觉模型。返回新 Report，原 Report 不变（frozen）。"""
    criteria = {item.get("id"): str(item.get("criterion") or "") for item in acceptance or []}
    pending = [item["id"] for item in report.results if item["result"] == "unknown"]
    if not pending:
        return report
    sheet, note = contact_sheet(media_path, workdir)
    if sheet is None:
        return replace(report, note=note, degraded=True)
    try:
        result = provider.vision(
            image=sheet, criteria=[{"id": cid, "criterion": criteria.get(cid, "")} for cid in pending]
        )
    except ProviderError as exc:
        return replace(report, note=f"视觉判定失败：{exc}", degraded=True)
    judged = {item["id"]: item for item in result.verdicts}
    results = [
        {"id": item["id"], "result": judged[item["id"]]["result"], "evidence": judged[item["id"]]["evidence"]}
        if item["id"] in judged and item["result"] == "unknown"
        else item
        for item in report.results
    ]
    left = sum(1 for item in results if item["result"] == "unknown")
    return replace(
        report,
        results=results,
        verdict=_verdict(results),
        note=f"视觉判定 {result.model}：{len(pending) - left}/{len(pending)} 条判出（frames={VISION_FRAMES}）",
    )

