"""提示词规范校验（Seedance / 视频方案）。

规则来源是用户真实产线的两份原始资料，不是我编的：
- `AI漫剧/AI漫剧制作工作流执行手册.md` §4.0 通用规则、§6 严禁事项
- `AI漫剧/剧情/我在仙界当了一天大弟子/Seedance2.0终剪提示词.md`（终剪交付物实际格式）

定位：这是一层 **lint**，不是闸门。手册的经验是「判一版提示词至少要跑 2 发」，
所以违规不该直接拦死用户，但必须**当场说清楚缺什么**——否则就是拿钱去试错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 旧代/传统体系术语，手册 §5 明确禁止出现在任何交付物中
LEGACY_TERMS = (
    "lora",
    "controlnet",
    "checkpoint",
    "negative prompt",
    "负向提示词",
    "负 prompt",
    "midjourney",
    "--ar",
)

#: m/s 严禁措辞：手册 §6.2
FORBIDDEN_PHRASES = (
    "保持一致",
    "连续继承",
    "同上",
    "如上",
    "同前",
)

#: 分镜标签：`镜头1（0-3秒）`，允许全角括号与 - – ~ 三种连接符
SHOT_LABEL = re.compile(
    r"镜头\s*(\d+)\s*[（(]\s*(\d+(?:\.\d+)?)\s*[-–~至]\s*(\d+(?:\.\d+)?)\s*秒?\s*[）)]"
)

#: 参考图编号：参考图一 / 参考图1
REFERENCE_LABEL = re.compile(r"参考图\s*([一二三四五六七八九十]|\d+)")

_CN_NUMBERS = "一二三四五六七八九十"


@dataclass
class PromptCheck:
    code: str
    message: str
    severity: str = "warning"

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class PromptReport:
    problems: list[PromptCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_list(self) -> list[dict]:
        return [problem.as_dict() for problem in self.problems]


def _reference_index(label: str) -> int:
    if label in _CN_NUMBERS:
        return _CN_NUMBERS.index(label) + 1
    return int(label)


def check_video_prompt(
    text: str,
    *,
    duration_seconds: int,
    reference_count: int,
) -> PromptReport:
    """按产线规范检查一条视频提示词。返回的问题按严重程度降序。"""
    report = PromptReport()
    body = (text or "").strip()
    if not body:
        report.problems.append(PromptCheck("PROMPT_EMPTY", "提示词为空", "error"))
        return report

    # 1) 分镜标签：手册 §4.0「`镜头N（起-止秒）` 标签不能删」
    shots = SHOT_LABEL.findall(body)
    if not shots:
        report.problems.append(
            PromptCheck(
                "PROMPT_MISSING_SHOT_LABELS",
                "缺少 `镜头N（起-止秒）` 分镜标签；删掉它模型容易切第三人称，"
                "一镜到底也要照写分镜号",
                "error",
            )
        )
    else:
        numbers = [int(item[0]) for item in shots]
        if numbers != list(range(1, len(numbers) + 1)):
            report.problems.append(
                PromptCheck(
                    "PROMPT_SHOT_NUMBERS",
                    f"分镜编号不连续：{numbers}；应为 1..{len(numbers)}",
                )
            )
        total = sum(float(end) - float(start) for _, start, end in shots)
        if duration_seconds and abs(total - duration_seconds) > 0.6:
            report.problems.append(
                PromptCheck(
                    "PROMPT_DURATION_MISMATCH",
                    f"分镜时间加总 {total:g}s 与片段时长 {duration_seconds}s 不一致；"
                    "时间点只做节拍锚，加总必须等于片段时长",
                    "error",
                )
            )

    # 2) 参考图编号：手册 §6.2「参考图编号不得省略、不得合并缩写」
    labels = [_reference_index(item) for item in REFERENCE_LABEL.findall(body)]
    if reference_count and not labels:
        report.problems.append(
            PromptCheck(
                "PROMPT_MISSING_REFERENCE_LABELS",
                f"没有按「参考图一/二」逐张标注用途；本次冻结了 {reference_count} 张参考图，"
                "每张都要独立标注用途并用完整自然语言描述",
                "error",
            )
        )
    elif reference_count and max(labels) < reference_count:
        report.problems.append(
            PromptCheck(
                "PROMPT_REFERENCE_COVERAGE",
                f"只引用了参考图 1..{max(labels)}，本次冻结了 {reference_count} 张；"
                "编号必须与冻结槽位顺序一致且不得省略",
            )
        )

    # 3) 字幕：手册 §6.2「Seedance 一律不写入任何字幕内容，负面词明确『无字幕』」
    if "无字幕" not in body:
        report.problems.append(
            PromptCheck(
                "PROMPT_MISSING_NO_SUBTITLE",
                "结尾缺少「无字幕，避免生成任何文字或字幕」；不写这一句模型会自己补字幕",
                "error",
            )
        )

    # 4) 严禁措辞
    lowered = body.lower()
    hit = [phrase for phrase in FORBIDDEN_PHRASES if phrase in body]
    if hit:
        report.problems.append(
            PromptCheck(
                "PROMPT_FORBIDDEN_PHRASE",
                f"出现严禁措辞：{'、'.join(hit)}；m/s 严禁「保持一致」「连续继承」"
                "以及「同上」「如上」这类省略指代",
                "error",
            )
        )
    legacy = [term for term in LEGACY_TERMS if term in lowered]
    if legacy:
        report.problems.append(
            PromptCheck(
                "PROMPT_LEGACY_TERM",
                f"出现旧代体系术语：{'、'.join(legacy)}；k/q/m/s 与旧体系不可混用",
                "error",
            )
        )

    # 5) 环境音：终剪交付物的固定段落
    if "环境音" not in body and "声音" not in body:
        report.problems.append(
            PromptCheck("PROMPT_MISSING_SOUNDSCAPE", "缺少环境音段落")
        )

    return report


__all__ = ["FORBIDDEN_PHRASES", "LEGACY_TERMS", "SHOT_LABEL", "check_video_prompt"]
