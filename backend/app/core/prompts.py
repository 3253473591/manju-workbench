"""隐藏预设、模板与提示词组装。

**边界（PRD §5.4）**：隐藏预设 A 与服务端组装/优化出的完整文本只在服务端流转，
不通过 API、来源查询或媒体正文返回。用户可见的只有 B（`PromptVersion.visible_text`）。
"""

from __future__ import annotations

import hashlib

from app.models.enums import HIDDEN_PRESET_VERSION

#: 图片方案的产品预设。A 是隐藏的，用户只能看到 B。
IMAGE_PRESETS: dict[str, str] = {
    "portrait": (
        "角色立绘：全身站姿，正面视角，服装与随身道具完整，重心自然，"
        "背景干净的浅灰底色，无文字、无水印"
    ),
    "face_closeup": (
        "面部特写：仅保留头饰与面部，五官清晰无遮挡，发丝细节完整，"
        "背景统一为干净的浅灰底色"
    ),
    "three_view": (
        "角色三视图：从左到右依次为正面全身站姿、纯侧面全身站姿、背面全身站姿，"
        "三个视角清晰可辨，背景统一为干净的浅灰底色，三格之间细白线分隔"
    ),
    "shot_frame": "分镜出图：按画面与动作描述构图，电影感光线，无字幕、无文字水印",
    "scene": "场景设定图：环境全景，交代空间关系与光线氛围，无人物，无文字",
    "prop": "道具设定图：单体居中，材质与细节清晰，背景干净的浅灰底色，无文字",
}

#: 立绘联合派生的隐藏固定模板（PRD §5.5）——直接生成，不自由改写、不人工选版。
FACE_CLOSEUP_TEMPLATE = (
    "生成人物脸部超清特写肖像，仅保留头饰与面部，展现面部细节，五官清晰无遮挡，"
    "发丝细节完整。背景统一为干净的浅灰底色。"
)
THREE_VIEW_TEMPLATE = (
    "生成角色三视图，从左到右分为三部分：第一部分为正面全身站姿视图，"
    "第二部分为纯侧面全身站姿视图，第三部分为背面全身站姿视图，三个视角清晰可辨。"
    "背景统一为干净的浅灰底色，三格之间有细白线分隔，人物在每个格子中独立呈现。"
)

DERIVE_TEMPLATES: dict[str, str] = {
    "face_closeup": FACE_CLOSEUP_TEMPLATE,
    "three_view": THREE_VIEW_TEMPLATE,
}

#: 文本任务的服务端系统提示词（不外传）
SYSTEM_PROMPTS: dict[str, str] = {
    "outline_optimize": (
        "你是漫剧编剧。根据用户给出的大纲正文与本次需求做优化或扩写，保持人物与世界观一致，"
        "输出简体中文。只输出 JSON，字段为 title 与 body。"
    ),
    "setting_generate": (
        "你是角色设定师。依据大纲正文产出人物设定草案，每个人物包含：姓名、身份、性格、"
        "外貌特征、发型与头饰、基础服装、服装变体、标志性随身道具。只输出 JSON，"
        "字段为 characters（数组，元素含 name/identity/personality/appearance/hair/"
        "baseOutfit/outfitVariants/signatureProps）。"
    ),
    "script_generate": (
        "你是漫剧编剧。依据大纲正文与已确认的人物设定写一集剧本，包含场景、动作与对白，"
        "输出简体中文。只输出 JSON，字段为 title 与 body。"
    ),
    "script_optimize": (
        "你是漫剧编剧。优化用户给出的剧本正文，保持绑定的人物设定一致，输出简体中文。"
        "只输出 JSON，字段为 title 与 body。"
    ),
    "script_split_clips": (
        "你是分镜师。把剧本正文拆成独立 Clip。每个 Clip 含编号、总时长（毫秒）、角色列表、"
        "场景名，以及内部镜头：编号、相对起止时间（毫秒）、画面与动作、景别与运镜、对白与音效。"
        "只输出 JSON，字段为 clips（数组）。"
    ),
    "clip_optimize": (
        "你是分镜师。优化单个 Clip 的画面与动作描述，保持时长与角色一致，输出简体中文。"
        "只输出 JSON，字段为 title、body、visual、camera、dialogue。"
    ),
    "plan_characters": (
        "你是美术指导。汇总给定 Clip 的人物，产出规范资产清单，做查重与规范命名，"
        "不要直接出图。只输出 JSON，字段为 assets（数组，元素含 assetType、canonicalName、"
        "variantName、imageKind、clipIds）。"
    ),
    "plan_scenes": (
        "你是美术指导。依据给定 Clip 规划场景资产，做查重与规范命名，不要直接出图。"
        "只输出 JSON，字段为 assets（数组，元素含 assetType、canonicalName、variantName、"
        "imageKind、clipIds）。"
    ),
    "plan_props": (
        "你是美术指导。依据给定 Clip 规划道具资产，做查重与规范命名，不要直接出图。"
        "只输出 JSON，字段为 assets（数组，元素含 assetType、canonicalName、variantName、"
        "imageKind、clipIds）。"
    ),
    "image_plan_optimize": (
        "你是图像提示词工程师。基于给定描述与本次需求，产出更具体的画面描述。"
        "只输出 JSON，字段为 visibleText。"
    ),
    "video_plan_create": (
        "你是漫剧终剪提示词作者。按 Seedance 2.0 的交付规范写中文提示词，"
        "整段必须是下面这个结构，段落之间空行：\n"
        "1) 参考图定义段：逐张写「将参考图一中的<内容>定义为场景：<完整自然语言描述>」、"
        "「参考图二为<主角名>的服装形制锚点：<服装写死到鞋>」、"
        "「将参考图三、参考图四定义为同一个<角色名>：<外形锚点>」。"
        "**编号必须与给定的冻结槽位顺序一致，一张都不能省，禁止合并缩写，"
        "禁止「同上」「如上」这类省略指代。**\n"
        "2) 视角段：写明全片视角与不变量（例如严格第一人称主观视角、一镜到底不切镜）。\n"
        "3) 分镜段：每镜一行，格式严格为「镜头N（起-止秒）：<画面与动作、运镜、对白>」，"
        "N 从 1 连续递增，**时间点加总必须等于片段总时长**，单镜头只留一种运镜。"
        "台词用 {大括号} 包住。\n"
        "4) 环境音段：以「环境音：」开头，列出脚步声、环境声与对白，并写明是否生成背景音乐。\n"
        "5) 收尾句：「画面高清，电影质感，<光线描述>，人物面部稳定不变形，"
        "动作自然流畅，无卡顿无闪烁；无字幕，避免生成任何文字或字幕。」\n"
        "严禁出现「保持一致」「连续继承」「同上」「如上」；"
        "严禁出现 lora / controlnet / checkpoint / 负向提示词等旧体系术语。"
        "只输出 JSON，字段为 visibleText。"
    ),
    "video_plan_optimize": (
        "你是漫剧终剪提示词作者。在已有提示词基础上按本次需求优化，"
        "**必须保留完整结构**：参考图定义段（编号与冻结槽位顺序一致、不得省略或缩写）、"
        "「镜头N（起-止秒）：」分镜段（时间点加总等于片段时长）、环境音段、"
        "以「无字幕，避免生成任何文字或字幕」收尾。"
        "严禁「保持一致」「连续继承」「同上」「如上」与旧体系术语。"
        "只输出 JSON，字段为 visibleText。"
    ),
}

#: 视频方案冻结后，提示词必须覆盖的结构要素（与 prompt_rules 校验项一一对应）
VIDEO_PROMPT_REQUIREMENTS = (
    "参考图逐张编号并标注用途，顺序与冻结槽位一致",
    "`镜头N（起-止秒）：` 分镜标签，编号连续",
    "分镜时间加总等于片段时长",
    "环境音段落",
    "以「无字幕，避免生成任何文字或字幕」收尾",
    "不得出现「保持一致」「连续继承」「同上」「如上」",
)


def compose_final_input(preset_key: str, visible_text: str) -> str:
    """服务端把隐藏 A 与用户可见 B 组装成最终生图/生视频输入。"""
    preset = IMAGE_PRESETS.get(preset_key, "")
    parts = [p for p in (preset, visible_text.strip()) if p]
    return "\n".join(parts)


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_fingerprint(*parts: object) -> str:
    import json

    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "DERIVE_TEMPLATES",
    "FACE_CLOSEUP_TEMPLATE",
    "HIDDEN_PRESET_VERSION",
    "IMAGE_PRESETS",
    "SYSTEM_PROMPTS",
    "THREE_VIEW_TEMPLATE",
    "compose_final_input",
    "content_fingerprint",
    "hash_text",
]
