"""把《师尊别砍我系统》第一集的真实产物解析成 docs/开发文档/01-数据结构.md 定义的结构。

    python scripts/seed_project.py                     # 解析 + 校验 + 输出 demo/seed/*.json
    python scripts/seed_project.py --load              # 解析 + 校验 + 输出 + 灌进 MySQL
    python scripts/seed_project.py --source <目录>      # 换一个剧集目录

权威性顺序（脚本里唯一允许"猜"的地方）：
    终剪提示词 > 剧本分镜 > 分集剧情
终剪提示词是真正发给模型的最终文本，时长/镜头/槽位都实测过；剧本是它之前的设计稿。
两边时长对不上时以终剪为准（剧本 Clip1「约 10s」vs 终剪 12.00s），差异只记 report，不报错。

只解析、只校验、只输出 JSON。`--load` 才连数据库，且那时才导入 `app.*` ——
纯解析路径保持零第三方依赖（stdlib 即可跑）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "apps" / "api" if (ROOT / "apps" / "api" / "manju").is_dir() else ROOT
DEFAULT_SOURCE = ROOT / "docs" / "我师父是个普通人"

_CN_DIGITS = "零一二三四五六七八九"


def cn_number(value: int) -> str:
    """1→一、10→十、12→十二、20→二十。只覆盖 1..99（`ck_ep_idx` 上限 50）。

    真实剧集目录是 `第一集` / `第二集` 这种中文数字命名，不转换就找不到第二集。
    """
    if value < 10:
        return _CN_DIGITS[value]
    tens, ones = divmod(value, 10)
    head = "十" if tens == 1 else _CN_DIGITS[tens] + "十"
    return head if ones == 0 else head + _CN_DIGITS[ones]


def episode_paths(source: Path, episode: int) -> dict[str, Path]:
    """集目录既可能是 `第二集`，也可能是 `第2集`，两种都认。"""
    for label in (f"第{cn_number(episode)}集", f"第{episode}集"):
        candidate = source / label
        if candidate.is_dir():
            return {
                "dir": candidate,
                "script": candidate / "剧本.md",
                "final": candidate / "Seedance2.0终剪提示词.md",
                "plot": source / f"{label}剧情.md",
            }
    label = f"第{cn_number(episode)}集"
    return {
        "dir": source / label,
        "script": source / label / "剧本.md",
        "final": source / label / "Seedance2.0终剪提示词.md",
        "plot": source / f"{label}剧情.md",
    }

# docs/开发文档/03-异常包容与边界值.md §6
DURATION_MIN = Decimal("1.00")
DURATION_MAX = Decimal("15.00")
SHOT_SUM_TOL = Decimal("0.05")
MAX_SHOTS = 8
MAX_CLIPS = 30

# 参考槽位容量是模型能力，不是全局常量（seedance-2.0 9 图 / 3 视频 / 3 音频；
# 2.5 系 30/10/10；出图模型锁 10 图）。真实第一集最多用到 6 张图，按 6 当上限会把能力关掉。
REF_CAPACITY = {
    "seedance-2.0": {"image": 9, "video": 3, "audio": 3},
    "seedance-2.5": {"image": 30, "video": 10, "audio": 10},
    "gemini-3.1-flash-image-preview": {"image": 10, "video": 0, "audio": 0},
    "gpt-image-2.5": {"image": 10, "video": 0, "audio": 0},
}
DEFAULT_MODEL_FAMILY = "seedance-2.0"

CN_NUM = "一二三四五六七八九十"

RE_H2 = re.compile(r"^##\s+(.+?)\s*$")
RE_H3 = re.compile(r"^###\s+(.+?)\s*$")
RE_FIELD = re.compile(r"^- \*\*([^*]+?)\*\*(.*)$")
RE_BULLET = re.compile(r"^\s+-\s+(.*)$")
RE_FENCE = re.compile(r"^\s*```")
RE_SHOT = re.compile(r"镜头(\d+)（(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)秒）[：:]")
RE_SLOT = re.compile(r"ref_(image|video|audio)_(\d+)=([^`\s]+)")
RE_REF_ITEM = re.compile(r"参考图([" + CN_NUM + r"]+)：`([^`]+)`（(.*)）$")
RE_TITLE_NORM = re.compile(r"[^\w\u4e00-\u9fff]+")

# 验收点在真实数据里没有统一写法，只认三种：
#   「开场禁空景」            —— 片段1 的结论行
#   验收点一：功德环全程在画   —— 片段2 的定义行
#   验收点一（已改版）：…      —— 片段7 的带括注定义行
RE_ACC_QUOTED = re.compile(r"验收点[一二三四五六七八九十]?(?:（[^）]*）)?[：:]?\s*[「『](.+?)[」』]")
RE_ACC_BOLD = re.compile(r"\*\*验收点[一二三四五六七八九十]?(?:（[^）]*）)?[：:]\s*([^*]+?)\*\*")
RE_DIALOGUE = re.compile(r"「([^」]{1,200})」|\{([^}]{1,200})\}")

# 剧本与终剪对同一个角色的叫法不同（师父/师尊、女主/徒弟、系统/系统拟人/小仙女）。
# 槽位回填靠词元重叠，所以两边都要先归一到同一套词。
ALIASES = (
    ("师尊", "师父"),
    ("女主", "徒弟"),
    ("系统拟人", "系统"),
    ("小仙女", "系统"),
)
RE_ASSET_PREFIX = re.compile(r"^\d{2}[^-]*-")


# ---------------------------------------------------------------- 结构


@dataclass
class Shot:
    no: int
    start: str
    end: str
    desc: str


@dataclass
class Slot:
    idx: int
    kind: str
    asset_ref: str
    role: str
    note: str
    script_ref: str | None = None  # 剧本里对应的参考资产名（回填 asset_id 用，见 §8.5）


@dataclass
class Dialogue:
    speaker: str
    text: str
    shot_no: int | None
    on_screen: bool


@dataclass
class Acceptance:
    id: str
    criterion: str
    superseded_by: str | None = None   # 真实数据里验收点会随版本改判（片段7）


@dataclass
class AcceptanceResult:
    id: str
    result: str
    evidence: str


@dataclass
class Revision:
    clip_key: str
    version_tag: str | None
    model_used: str | None
    resolution: str | None
    task_id: str | None
    input_tokens: int | None
    video_seconds: str | None
    unit_price: str | None
    cost: str | None
    latency_ms: int | None
    motion: dict | None
    acceptance_result: list[AcceptanceResult] = field(default_factory=list)


@dataclass
class Clip:
    clip_key: str
    idx: int                 # 播放顺序（1..N），不是源片段号
    source_idx: int          # 源文件里的「片段N」
    title: str
    inserted: bool
    duration_s: str
    prompt: str
    negative: str | None
    shots: list[Shot]
    ref_slots: list[Slot]
    dialogue: list[Dialogue]
    acceptance: list[Acceptance]
    core_plot: str
    camera_note: str | None
    source: dict
    delivered_stem: str | None = None  # 交付成片名主干（s7 / s6b），seed 用它找 output asset


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.info.append(msg)


class Field(NamedTuple):
    base: str        # 实测经验 / 台词 / 注意 / 核心剧情
    meta: str        # 括号里的说明（可能在加粗内，也可能在加粗后）
    value: str       # 冒号后的正文


# ---------------------------------------------------------------- 基础解析


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def dec(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def quant2(value: str) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def norm_title(title: str) -> str:
    return RE_TITLE_NORM.sub("", title.split("——")[0])


def strip_paren(text: str) -> tuple[str, str]:
    """拆掉开头的（…），返回 (括号内容, 剩余)。括号可嵌套。"""
    if not text.startswith("（"):
        return "", text
    depth = 0
    for i, ch in enumerate(text):
        if ch == "（":
            depth += 1
        elif ch == "）":
            depth -= 1
            if depth == 0:
                return text[1:i], text[i + 1 :]
    return text[1:], ""


def parse_field(line: str) -> Field | None:
    m = RE_FIELD.match(line)
    if not m:
        return None
    label, tail = m.group(1), m.group(2)
    meta = ""
    if "（" in label:
        i = label.index("（")
        close = label.rfind("）")
        meta = label[i + 1 : close] if close > i else label[i + 1 :]
        label = label[:i]
    tail_meta, tail = strip_paren(tail.lstrip())
    tail = tail.lstrip().lstrip("：:").lstrip()
    return Field(base=label.strip(), meta=(meta or tail_meta).strip(), value=tail.strip())


def split_by_heading(lines: list[str], heading: re.Pattern[str]) -> list[tuple[int, str, list[str]]]:
    hits = [(i, m.group(1)) for i, line in enumerate(lines) if (m := heading.match(line))]
    out = []
    for n, (start, title) in enumerate(hits):
        end = hits[n + 1][0] if n + 1 < len(hits) else len(lines)
        out.append((start + 1, title, lines[start + 1 : end]))
    return out


def collect_fields(body: list[str]) -> dict[str, Field]:
    """顶层 `- **字段**` 块。同名只保留第一次出现（实测经验另行按顺序扫）。"""
    out: dict[str, Field] = {}
    for line in body:
        if line.startswith(">"):
            continue
        f = parse_field(line)
        if f:
            out.setdefault(f.base, f)
    return out


def bullets_of(body: list[str], base: str) -> tuple[Field | None, list[str]]:
    """返回某字段的值与它下面的缩进子弹行。"""
    field_hit: Field | None = None
    bullets: list[str] = []
    for line in body:
        if line.startswith(">"):
            continue
        if not line.startswith(" "):
            f = parse_field(line)
            if f:
                field_hit = f if f.base == base else None
            elif line.strip():
                field_hit = None
            continue
        if field_hit is None:
            continue
        m = RE_BULLET.match(line)
        if m:
            bullets.append(m.group(1).strip())
    return field_hit, bullets


def quoted(text: str) -> str | None:
    for left, right in (("「", "」"), ("“", "”"), ('"', '"')):
        i = text.find(left)
        if i < 0:
            continue
        j = text.find(right, i + 1)
        if j > i:
            return text[i + 1 : j].strip()
    return None


# ---------------------------------------------------------------- 片段内结构


def parse_shots(prompt: str) -> list[Shot]:
    marks = list(RE_SHOT.finditer(prompt))
    shots = []
    for n, m in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(prompt)
        desc = re.sub(r"\s+", " ", prompt[m.end() : end]).strip()
        shots.append(Shot(no=int(m.group(1)), start=m.group(2), end=m.group(3), desc=desc))
    return shots


def role_of(note: str) -> str:
    if "实景" in note or "唯一场景" in note:
        return "实景"
    if "不定义构图" in note:
        return "造型锚"
    if "形态参考" in note or "形态锚" in note:
        return "形态参考"
    if "构图" in note:
        return "构图锚"
    return "造型锚"


def parse_slots(ref_field: Field | None, slot_field: Field | None, report: Report, where: str) -> list[Slot]:
    files: dict[tuple[str, int], str] = {}
    if slot_field:
        for m in RE_SLOT.finditer(slot_field.value):
            files[(m.group(1), int(m.group(2)) + 1)] = m.group(3)
    notes: dict[int, tuple[str, str]] = {}
    if ref_field:
        for chunk in re.split(r"(?=参考图[" + CN_NUM + r"]+：)", ref_field.value):
            chunk = chunk.strip().rstrip("；;。")
            m = RE_REF_ITEM.match(chunk)
            if not m or len(m.group(1)) != 1:
                if chunk:
                    report.warn(f"{where}: 参考图条目无法解析：{chunk[:40]}")
                continue
            notes[CN_NUM.index(m.group(1)) + 1] = (m.group(2), m.group(3))

    slots: list[Slot] = []
    for kind in ("image", "video", "audio"):
        idxs = sorted({i for k, i in files if k == kind} | ({i for i in notes} if kind == "image" else set()))
        for expected, idx in enumerate(idxs, start=1):
            if idx != expected:
                report.err(f"{where}: {kind} 槽位断号，期望 {expected} 实际 {idx}")
            name, note = notes.get(idx, ("", "")) if kind == "image" else ("", "")
            slots.append(
                Slot(idx=idx, kind=kind, asset_ref=files.get((kind, idx)) or name, role=role_of(note), note=note[:500])
            )
    if slot_field and ref_field and len(files) != len(notes):
        report.note(f"{where}: 槽位 {len(files)} 个 / 参考图说明 {len(notes)} 条（允许不等，以槽位为准）")
    return slots


def parse_dialogue(bullets: list[str], meta: str) -> list[Dialogue]:
    off_screen = "无口型" in meta or "画外" in meta
    out = []
    for bullet in bullets:
        text = quoted(bullet)
        if not text:
            continue
        head = bullet.split("「")[0].split("“")[0].split('"')[0]
        shot = re.search(r"镜头\s*(\d+)", head)
        speaker = re.split(r"[（，,：:]", head)[0].strip() or "unknown"
        out.append(
            Dialogue(
                speaker=speaker,
                text=text,
                shot_no=int(shot.group(1)) if shot else None,
                on_screen=not (off_screen or "无口型" in bullet or "画外" in bullet),
            )
        )
    return out


def acceptance_hits(line: str) -> list[str]:
    hits = [m.group(1) for m in RE_ACC_QUOTED.finditer(line)]
    hits += [m.group(1).strip() for m in RE_ACC_BOLD.finditer(line)]
    return [h for h in hits if h]


def parse_acceptance(body: list[str]) -> list[Acceptance]:
    seen: dict[str, str] = {}
    for line in body:
        for crit in acceptance_hits(line):
            seen.setdefault(crit, f"验收点「{crit}」")
    return [Acceptance(id=f"a{n}", criterion=text) for n, text in enumerate(seen.values(), start=1)]


def parse_acceptance_result(block: list[str], criteria: dict[str, str]) -> list[AcceptanceResult]:
    out = []
    for line in block:
        failed = "未达成" in line or line.lstrip().startswith("- ❌")
        for crit in acceptance_hits(line):
            cid = criteria.get(crit)
            if cid is None:
                continue
            out.append(
                AcceptanceResult(id=cid, result="fail" if failed else "pass", evidence="未达成" if failed else "达成")
            )
    return out


def dialogue_from_prompt(prompt: str) -> list[Dialogue]:
    """台词段缺失时的兜底：直接抄提示词正文里的「…」与 {…}。

    真实数据里片段3 就没有 `- **台词**` 段，三句词全内嵌在镜头正文里（#94：正文没词模型就自己编）。
    """
    marks = [(m.start(), int(m.group(1))) for m in RE_SHOT.finditer(prompt)]
    found: list[tuple[int, str]] = []
    for m in RE_DIALOGUE.finditer(prompt):
        text = (m.group(1) or m.group(2) or "").strip()
        if text:
            found.append((m.start(), text))
    out = []
    for pos, text in sorted(found):
        shot = next((no for start, no in reversed(marks) if start < pos), None)
        out.append(Dialogue(speaker="unknown", text=text, shot_no=shot, on_screen=True))
    return out


def parse_motion(text: str) -> dict | None:
    samples = re.search(r"(\d+)\s*个?采样点", text)
    freeze = re.search(r"FREEZE\s*=\s*(\d+)", text)
    avg = re.search(r"avg\s*([\d.]+)", text)
    peak = re.search(r"max\s*([\d.]+)", text)
    if not any((samples, freeze, avg, peak)):
        return None
    return {
        "samples": int(samples.group(1)) if samples else None,
        "freeze": int(freeze.group(1)) if freeze else None,
        "avg": float(avg.group(1)) if avg else None,
        "max": float(peak.group(1)) if peak else None,
    }


# ---------------------------------------------------------------- 终剪提示词


def parse_final_prompts(path: Path, report: Report, *, episode: int = 1) -> tuple[list[Clip], list[Revision]]:
    lines = read_lines(path)
    clips: list[Clip] = []
    revisions: list[Revision] = []
    orders: dict[int, tuple[int, str]] = {}
    for line_no, heading, body in split_by_heading(lines, RE_H2):
        m = re.match(r"片段(\d+)：(.+?)（时长：([\d.]+)s）", heading)
        if not m:
            continue
        idx, name, duration = int(m.group(1)), m.group(2), m.group(3)
        where = f"{path.name}:{line_no} 片段{idx}"

        prompt_lines: list[str] = []
        in_fence = False
        for line in body:
            if RE_FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                prompt_lines.append(line)
        prompt = "\n".join(prompt_lines).strip()
        if not prompt:
            report.err(f"{where}: 没有找到提示词代码块")

        fields = collect_fields(body)
        _, dialogue_bullets = bullets_of(body, "台词")
        dialogue_meta = fields["台词"].meta if "台词" in fields else ""
        dialogue = parse_dialogue(dialogue_bullets, dialogue_meta)
        if not dialogue and prompt:
            dialogue = dialogue_from_prompt(prompt)
            if dialogue:
                report.note(f"{where}: 没有 `台词` 段，从提示词正文抽出台词 {len(dialogue)} 条")
        for d in dialogue:
            if d.text not in prompt:
                report.warn(f"{where}: 台词「{d.text[:14]}…」没有内嵌进提示词正文（#94）")
        acceptance = parse_acceptance(body)
        criteria = {a.criterion.removeprefix("验收点「").removesuffix("」"): a.id for a in acceptance}

        clip = Clip(
            clip_key=f"ep{episode}.s{idx}",
            idx=0,                      # 排序后回填，见下方 enumerate
            source_idx=idx,
            title=name,
            inserted=False,
            duration_s=quant2(duration),
            prompt=prompt,
            negative=_extract_negative(prompt),
            shots=parse_shots(prompt),
            ref_slots=parse_slots(fields.get("参考图"), fields.get("参考图槽位"), report, where),
            dialogue=dialogue,
            acceptance=acceptance,
            core_plot=fields["核心剧情"].value if "核心剧情" in fields else "",
            camera_note=None,
            source={"final_prompt": path.name, "line": line_no},
        )
        clips.append(clip)
        orders[idx] = _delivery_order(fields, idx)
        revisions.extend(_parse_revisions(body, clip.clip_key, criteria, report, where))
        report.note(
            f"{where}: {len(clip.shots)} 镜 / {len(clip.ref_slots)} 槽位 / "
            f"{len(clip.dialogue)} 台词 / {len(clip.acceptance)} 验收点"
        )

    clips.sort(key=lambda c: orders[c.source_idx])
    for position, clip in enumerate(clips, start=1):
        clip.idx = position
        number, suffix = orders[clip.source_idx]
        clip.delivered_stem = f"s{number}{suffix}"
        if position != clip.source_idx:
            report.note(f"片段{clip.source_idx}「{clip.title}」实际播放第 {position} 位（播放序 ≠ 源序号）")
    return clips, revisions


def _delivery_order(fields: dict[str, Field], source_idx: int) -> tuple[int, str]:
    """播放顺序取自交付名 `第一集\\s6b_徒弟的自嘲反应.mp4`；没有交付名就按源片段号。

    真实数据里片段8 是插入镜，文件里排在片段7 之前，但交付名是 `s6b_`，播放位置在 s6 之后。
    """
    delivery = fields.get("作业清单 / 定稿素材")
    if delivery:
        m = re.search(r"s(\d+)([a-z]?)_", delivery.value)
        if m:
            return (int(m.group(1)), m.group(2))
    return (source_idx, "")


def _extract_negative(prompt: str) -> str | None:
    m = re.search(r"负面提示[：:]\s*(.+?)(?:\n|$)", prompt)
    return m.group(1).strip() if m else None


def _parse_revisions(
    body: list[str], clip_key: str, criteria: dict[str, str], report: Report, where: str
) -> list[Revision]:
    revisions: list[Revision] = []
    header: Field | None = None
    block: list[str] = []

    def flush() -> None:
        if header is None:
            return
        revision = _new_revision(header, clip_key, [RE_BULLET.match(line).group(1) for line in block])
        revision.acceptance_result = parse_acceptance_result(block, criteria)
        revisions.append(revision)

    for line in body:
        if line.startswith(">"):
            continue
        f = parse_field(line)
        if f and f.base == "实测经验":
            flush()
            header = f
            block = []
            continue
        if header is not None and RE_BULLET.match(line):
            block.append(line)
    flush()
    if not revisions:
        report.warn(f"{where}: 没有实测经验块，成本/动线字段留空")
    return revisions


def _new_revision(f: Field, clip_key: str, bullets: list[str]) -> Revision:
    # 有的块把 TASK_ID/用量/动线写在标题行后面，有的整段都在子项里 —— 两处拼起来再抽
    text = "\n".join([f.value, *bullets])
    version = re.search(r"(v\d+)", f.meta)
    resolution = re.search(r"(\d+p)", f.meta)
    task = re.search(r"TASK_ID\s*`([^`]+)`", text)
    seconds = re.search(r"=\s*(\d+)s\s*×", text) or re.search(r"·\s*(\d+)s\s*·", f.meta)
    tokens = re.search(r"total_tokens\s*=\s*([\d,]+)", text)
    cost = re.search(r"≈\s*([\d.]+)\s*元", text)
    price = re.search(r"([\d.]+)\s*元/秒", text)
    minutes = re.search(r"约\s*([\d.]+)\s*分钟", text)
    return Revision(
        clip_key=clip_key,
        version_tag=version.group(1) if version else None,
        model_used="seedance-2.0-mini" if "mini" in f.meta else None,
        resolution=resolution.group(1) if resolution else None,
        task_id=task.group(1) if task else None,
        input_tokens=int(tokens.group(1).replace(",", "")) if tokens else None,
        video_seconds=seconds.group(1) if seconds else None,
        unit_price=price.group(1) if price else None,
        cost=cost.group(1) if cost else None,
        latency_ms=int(round(float(minutes.group(1)) * 60_000)) if minutes else None,
        motion=parse_motion(text),
    )


# ---------------------------------------------------------------- 剧本分镜


def parse_script(path: Path, report: Report) -> tuple[dict, list[dict]]:
    lines = read_lines(path)
    meta: dict = {}
    for line in lines:
        f = parse_field(line)
        if f is None:
            continue
        if f.base == "剧名":
            meta["title"] = f.value.strip("《》")
        elif f.base == "集数":
            meta["episode_label"] = f.value.strip()
        elif f.base == "画幅":
            meta["aspect"] = f.value.strip()
        elif f.base == "画风基调":
            meta["style"] = f.value.strip()
        elif f.base == "预估总时长":
            sec = re.search(r"(\d+)\s*秒", f.value)
            cnt = re.search(r"共\s*(\d+)\s*个\s*Clip", f.value)
            meta["planned_total_seconds"] = int(sec.group(1)) if sec else None
            meta["planned_clip_count"] = int(cnt.group(1)) if cnt else None

    clips: list[dict] = []
    for line_no, heading, body in split_by_heading(lines, RE_H3):
        m = re.match(r"Clip\s*(\d+)：(.+)$", heading)
        if not m:
            continue
        idx, rest = int(m.group(1)), m.group(2).split("——")[0]
        dur = re.search(r"（约\s*([\d.]+)\s*s）", rest)
        name = re.sub(r"（约\s*[\d.]+\s*s）", "", rest).strip()
        fields = collect_fields(body)

        ref_assets = []
        for bullet in bullets_of(body, "参考资产")[1]:
            am = re.match(r"参考图([" + CN_NUM + r"]+)：`([^`]+)`", bullet)
            if am:
                ref_assets.append({"ordinal": am.group(1), "name": am.group(2)})

        shots = []
        for bullet in bullets_of(body, "镜头时序")[1]:
            sm = re.match(r"`镜头(\d+)（(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)秒）`[：:]\s*(.*)$", bullet)
            if sm:
                shots.append({"no": int(sm.group(1)), "start": sm.group(2), "end": sm.group(3), "desc": sm.group(4).strip()})

        clips.append(
            {
                "idx": idx,
                "title": name,
                "duration_s": quant2(dur.group(1)) if dur else None,
                "core_plot": fields["核心剧情"].value if "核心剧情" in fields else "",
                "ref_assets": ref_assets,
                "shots": shots,
                "dialogue": bullets_of(body, "台词与声音")[1],
                "camera_note": fields["运镜"].value if "运镜" in fields else None,
                "line": line_no,
            }
        )
    if not clips:
        report.err(f"{path.name}: 没有解析到任何 `### Clip N` 段落")
    return meta, clips


# ---------------------------------------------------------------- 合并 + 校验


def _normalize(text: str) -> str:
    for source, target in ALIASES:
        text = text.replace(source, target)
    return text


def _tokens(name: str) -> list[str]:
    """`00人物-师父-喜剧版-肯德基滑板` → ['师父', '喜剧版', '肯德基滑板']。先去掉 `NN类别-` 前缀再切。"""
    stem = RE_ASSET_PREFIX.sub("", name).rsplit(".", 1)[0]
    return [token for token in re.split(r"[-_－与和]", _normalize(stem)) if len(token) >= 2]


def _hits(token: str, note: str) -> bool:
    """整词命中，或长词的核心部分命中（`四十米黑气巨刃` → `黑气巨刃`）。"""
    return token in note or (len(token) >= 5 and token[-4:] in note)


def match_script_refs(script_assets: list[dict], slots: list[Slot], report: Report, where: str) -> None:
    """把剧本的「参考图一/二/三」对到终剪的槽位上（`01` §8.5）。

    **不按序号硬对**：剧本 Clip1 只有 3 个资产、终剪有 4 个槽位，序号在第 3 个就错位了
    （剧本第 3 个是"系统界面"，而终剪第 3 槽是母猪妖）。改按词元重叠匹配，
    序号只在多个候选并列时用来破平；匹配不上就留空并记警告 —— 宁可没有，也不要挂错图。
    """
    taken: set[int] = set()
    for asset in script_assets:
        tokens = _tokens(asset["name"])
        candidates = [
            slot for slot in slots if any(_hits(token, _normalize(slot.note)) for token in tokens)
        ]
        if not candidates:
            report.warn(f"{where}: 剧本参考资产「{asset['name']}」对不上任何槽位，留空")
            continue
        free = [slot for slot in candidates if slot.idx not in taken]
        if not free:
            report.warn(f"{where}: 剧本参考资产「{asset['name']}」的候选槽位已被占用，留空")
            continue
        ordinal = CN_NUM.index(asset["ordinal"]) + 1 if len(asset["ordinal"]) == 1 else None
        chosen = next((slot for slot in free if slot.idx == ordinal), free[0] if len(free) == 1 else None)
        if chosen is None:
            report.warn(
                f"{where}: 剧本参考资产「{asset['name']}」命中多个槽位 {[slot.idx for slot in free]}，留空"
            )
            continue
        chosen.script_ref = asset["name"]
        taken.add(chosen.idx)


def merge(final_clips: list[Clip], script_clips: list[dict], report: Report) -> None:
    by_title = {norm_title(c["title"]): c for c in script_clips}
    used: set[str] = set()
    for clip in final_clips:
        key = norm_title(clip.title)
        script = by_title.get(key)
        if script is None:
            clip.inserted = True
            report.warn(f"片段{clip.idx}「{clip.title}」在剧本中无对应，按插入镜处理（inserted=true）")
            continue
        used.add(key)
        clip.camera_note = script["camera_note"]
        clip.source["script_line"] = script["line"]
        planned, actual = dec(script["duration_s"]), dec(clip.duration_s)
        if planned is not None and actual is not None and planned != actual:
            report.note(f"片段{clip.idx}: 剧本计划 {planned}s / 终剪实际 {actual}s，以终剪为准（差 {actual - planned}s）")
        if len(script["dialogue"]) != len(clip.dialogue):
            report.note(
                f"片段{clip.idx}: 剧本台词 {len(script['dialogue'])} 条 / 终剪 {len(clip.dialogue)} 条（终剪为改词后版本）"
            )
        match_script_refs(script["ref_assets"], clip.ref_slots, report, f"片段{clip.idx}")
        matched = sum(1 for slot in clip.ref_slots if slot.script_ref)
        report.note(f"片段{clip.idx}: 槽位回填 {matched}/{len(clip.ref_slots)}（来自剧本参考资产）")
    for key, script in by_title.items():
        if key not in used:
            report.err(f"剧本 Clip {script['idx']}「{script['title']}」在终剪提示词中无对应")


def validate(clips: list[Clip], report: Report, model_family: str) -> None:
    capacity = REF_CAPACITY[model_family]
    if len(clips) > MAX_CLIPS:
        report.err(f"片段数 {len(clips)} 超过上限 {MAX_CLIPS}")
    seen: set[int] = set()
    for clip in clips:
        where = f"片段{clip.idx}"
        if clip.idx in seen:
            report.err(f"{where}: 播放序号重复")
        seen.add(clip.idx)

        duration = dec(clip.duration_s)
        if duration is None:
            report.err(f"{where}: 时长无法解析")
        elif not (DURATION_MIN <= duration <= DURATION_MAX):
            report.err(f"{where}: 时长 {duration}s 超出 {DURATION_MIN}–{DURATION_MAX}s")

        if not clip.shots:
            report.err(f"{where}: 提示词里没有 `镜头N（起-止秒）：` 标记，shots 为空")
        else:
            if len(clip.shots) > MAX_SHOTS:
                report.err(f"{where}: {len(clip.shots)} 个镜头，超过上限 {MAX_SHOTS}")
            if dec(clip.shots[0].start) != Decimal("0"):
                report.err(f"{where}: 首镜 start={clip.shots[0].start}，应为 0")
            for a, b in zip(clip.shots, clip.shots[1:]):
                if dec(a.end) != dec(b.start):
                    report.err(f"{where}: 镜头{a.no} end={a.end} 与 镜头{b.no} start={b.start} 不衔接")
            last = dec(clip.shots[-1].end)
            if last is not None and duration is not None and abs(last - duration) > SHOT_SUM_TOL:
                report.err(f"{where}: 末镜 end={last}s 与标称时长 {duration}s 差 {abs(last - duration)}s")

        counts = Counter(s.kind for s in clip.ref_slots)
        for kind, used in counts.items():
            if used > capacity[kind]:
                report.err(f"{where}: {used} 个 {kind} 参考超 {model_family} 的容量 {capacity[kind]}")

        shot_nos = {s.no for s in clip.shots}
        for d in clip.dialogue:
            if d.shot_no is not None and d.shot_no not in shot_nos:
                report.err(f"{where}: 台词「{d.text[:12]}…」指向不存在的镜头 {d.shot_no}")
            if len(d.text) > 200:
                report.warn(f"{where}: 台词 {len(d.text)} 字，超过建议上限 200")
        if not clip.dialogue:
            report.note(f"{where}: 无台词（纯画面片段）")
        if not clip.acceptance:
            report.warn(f"{where}: 没有可提取的验收点，质检退化为动线提示")

    peak = {
        kind: max((sum(1 for s in c.ref_slots if s.kind == kind) for c in clips), default=0)
        for kind in ("image", "video", "audio")
    }
    report.note(
        f"参考槽位峰值 图{peak['image']}/{capacity['image']} "
        f"视频{peak['video']}/{capacity['video']} 音频{peak['audio']}/{capacity['audio']}（{model_family}）"
    )


# ---------------------------------------------------------------- 资产


def scan_assets(source: Path, report: Report) -> list[dict]:
    kinds = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".mp4": "video", ".mov": "video"}
    out: list[dict] = []
    for root in ("00_静态资源", "人物卡/results", "第一集", "第二集"):
        base = source / root
        if not base.exists():
            report.warn(f"资产目录不存在：{root}")
            continue
        for path in sorted(base.rglob("*")):
            kind = kinds.get(path.suffix.lower()) if path.is_file() else None
            if kind is None:
                continue
            name = path.name
            role_hint = (
                "character" if name.startswith("00人物")
                else "scene" if name.startswith("01场景")
                else "prop" if name.startswith("02道具")
                else "clip" if kind == "video"
                else None
            )
            out.append(
                {
                    "rel_path": path.relative_to(source).as_posix(),
                    "kind": kind,
                    "bytes": path.stat().st_size,
                    "role_hint": role_hint,
                    "source": "seed",
                }
            )
    return out


def resolve_slots(clips: list[Clip], assets: list[dict], report: Report) -> None:
    known = {Path(a["rel_path"]).name.lower(): a["rel_path"] for a in assets}
    missing: set[str] = set()
    for clip in clips:
        for slot in clip.ref_slots:
            name = Path(slot.asset_ref).name.lower()
            if name in known:
                slot.asset_ref = known[name]
            else:
                missing.add(slot.asset_ref)
    if missing:
        report.warn(
            f"{len(missing)} 个槽位文件不在本目录（是 _krea2_pipeline 的作业副本）："
            + "、".join(sorted(missing))
            + " —— W1 需由资产库按导出作业回填 asset_id"
        )


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析真实剧集目录，输出 seed JSON 并校验数据结构")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--episode", type=int, default=1, help="集号：决定读 第N集/剧本.md 与 第N集剧情.md")
    parser.add_argument("--out", type=Path, default=ROOT / "demo" / "seed")
    parser.add_argument("--load", action="store_true", help="解析后灌进数据库（需要 alembic upgrade head 已跑过）")
    parser.add_argument("--thumbs", action="store_true", help="灌库时用 ffmpeg 抽视频首帧当海报（写 MEDIA_ROOT/.thumbs/）")
    parser.add_argument("--dsn", help="覆盖 DATABASE_URL")
    args = parser.parse_args(argv)

    report = Report()
    source: Path = args.source
    paths = {**episode_paths(source, args.episode), "setting": source / "人物设定.md"}
    for name, path in paths.items():
        if not path.exists():
            report.err(f"缺少源文件（{name}）：{path}")
    if report.errors:
        _dump_report(report)
        return 2

    meta, script_clips = parse_script(paths["script"], report)
    clips, revisions = parse_final_prompts(paths["final"], report, episode=args.episode)
    merge(clips, script_clips, report)
    model_family = _family(next((r.model_used for r in revisions if r.model_used), None))
    validate(clips, report, model_family)
    assets = scan_assets(source, report)
    resolve_slots(clips, assets, report)

    characters = []
    for line in read_lines(paths["setting"]):
        clean = line.replace("**", "").strip()
        if not clean:
            continue
        name, _, anchor = clean.partition("：")
        characters.append({"name": name.strip(), "anchor": anchor.strip()})

    project = {
        "title": meta.get("title"),
        "genre": "修仙荒诞反差喜剧",
        "tone": meta.get("style"),
        "aspect": meta.get("aspect"),
        "logline": "｜".join(f"{c['name']}：{c['anchor']}" for c in characters)[:500],
        "characters": characters,
        "planned_total_seconds": meta.get("planned_total_seconds"),
        "planned_clip_count": meta.get("planned_clip_count"),
    }
    episodes = [
        {
            "idx": args.episode,
            "title": meta.get("episode_label") or f"第{args.episode}集",
            "synopsis": paths["plot"].read_text(encoding="utf-8").strip(),
            "status": "storyboarded",
            "clip_count": len(clips),
        }
    ]

    args.out.mkdir(parents=True, exist_ok=True)
    _write(args.out / "project.json", project)
    _write(args.out / "episodes.json", episodes)
    _write(args.out / "clips.json", [asdict(c) for c in clips])
    _write(args.out / "assets.json", assets)
    _write(args.out / "revisions.json", [asdict(r) for r in revisions])
    _write(args.out / "script_clips.json", script_clips)
    _dump_report(report, args.out / "report.json")

    with_acceptance = sum(1 for c in clips if c.acceptance)
    report.note(f"验收点覆盖 {with_acceptance}/{len(clips)} 个片段（其余片段只写了散文式结论，无法机检）")

    print(
        f"episodes={len(episodes)} clips={len(clips)} "
        f"shots={sum(len(c.shots) for c in clips)} slots={sum(len(c.ref_slots) for c in clips)} "
        f"dialogue={sum(len(c.dialogue) for c in clips)} acceptance={sum(len(c.acceptance) for c in clips)} "
        f"revisions={len(revisions)} assets={len(assets)} "
        f"errors={len(report.errors)} warnings={len(report.warnings)}"
    )
    print(f"输出：{args.out}")
    if args.thumbs and not args.load:
        report.err("--thumbs 只能和 --load 一起用：海报是写入 MEDIA_ROOT 的副作用，不是解析产物")
        _dump_report(report)
        return 1
    if args.load and not _load_into_db(args.out, source, args.dsn, args.thumbs, report):
        return 1
    return 1 if report.errors else 0


def _load_into_db(seed_dir: Path, media_root: Path, dsn: str | None, thumbs: bool, report: Report) -> bool:
    sys.path.insert(0, str(API_ROOT))
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session

    from manju.db import make_engine
    from manju.seed import load_seed

    try:
        with Session(make_engine(dsn)) as session:
            stats = load_seed(session, seed_dir, media_root, thumbs=thumbs)
    except (SQLAlchemyError, ValueError) as exc:
        report.err(f"灌库失败：{type(exc).__name__}: {exc}")
        _dump_report(report, seed_dir / "report.json")
        return False
    print("灌库：" + " ".join(f"{key}={value}" for key, value in stats.items()))
    return True


def _family(model: str | None) -> str:
    """`seedance-2.0-mini` → `seedance-2.0`；未知模型按默认系列算。"""
    if not model:
        return DEFAULT_MODEL_FAMILY
    for family in REF_CAPACITY:
        if model.startswith(family):
            return family
    return DEFAULT_MODEL_FAMILY


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _dump_report(report: Report, path: Path | None = None) -> None:
    for label, items in (("ERROR", report.errors), ("WARN", report.warnings), ("INFO", report.info)):
        for item in items:
            print(f"[{label}] {item}")
    if path:
        _write(path, {"errors": report.errors, "warnings": report.warnings, "info": report.info})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
