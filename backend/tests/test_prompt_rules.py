"""视频提示词的产线规范校验。

规则来源是用户真实产线的原始资料，不是编的：
`AI漫剧/AI漫剧制作工作流执行手册.md` §4.0 / §6，以及
`AI漫剧/剧情/我在仙界当了一天大弟子/Seedance2.0终剪提示词.md` 的实际交付格式。
"""

from __future__ import annotations

from app.services.prompt_rules import check_video_prompt

#: 照真实终剪交付物的结构写一条合规提示词
COMPLIANT = """将参考图一中的雨夜校门定义为场景：湿漉漉的青灰石板、路侧石栏与雨幕反光。
参考图二为林晚的服装形制锚点：深藏青校服外套配白色衬衫，形制写死到鞋。
将参考图三、参考图四定义为同一个沈知夏：黑长直、银色发带。

全片为第三人称叙述视角，一镜到底、不切镜、不反打。

镜头1（0-3秒）：雨幕压低整条街，林晚撑伞立于画面中央，镜头缓慢推近。
镜头2（3-5秒）：对方从雨里走出，镜头横移跟随，情绪收在最后一拍。

环境音：雨声、脚步声与伞面水珠声，不生成背景音乐。

画面高清，电影质感，柔和侧光；人物面部稳定不变形，动作自然流畅，无卡顿无闪烁；无字幕，避免生成任何文字或字幕。"""


def test_compliant_prompt_passes() -> None:
    report = check_video_prompt(COMPLIANT, duration_seconds=5, reference_count=4)
    assert report.ok, report.as_list()


def test_missing_shot_labels_is_an_error() -> None:
    text = COMPLIANT.replace("镜头1（0-3秒）：", "开场：").replace("镜头2（3-5秒）：", "收束：")
    report = check_video_prompt(text, duration_seconds=5, reference_count=4)
    codes = {problem.code for problem in report.problems}
    assert "PROMPT_MISSING_SHOT_LABELS" in codes


def test_duration_sum_must_match_clip_length() -> None:
    report = check_video_prompt(COMPLIANT, duration_seconds=12, reference_count=4)
    codes = {problem.code for problem in report.problems}
    # 提示词分镜加总 5s，与片段 12s 不符
    assert "PROMPT_DURATION_MISMATCH" in codes


def test_shot_numbers_must_be_continuous() -> None:
    text = COMPLIANT.replace("镜头2（3-5秒）：", "镜头4（3-5秒）：")
    report = check_video_prompt(text, duration_seconds=5, reference_count=4)
    codes = {problem.code for problem in report.problems}
    assert "PROMPT_SHOT_NUMBERS" in codes


def test_reference_labels_are_required() -> None:
    text = COMPLIANT.replace("参考图一", "那张图").replace("参考图二", "另一张").replace(
        "参考图三、参考图四", "剩下的图"
    )
    report = check_video_prompt(text, duration_seconds=5, reference_count=4)
    codes = {problem.code for problem in report.problems}
    assert "PROMPT_MISSING_REFERENCE_LABELS" in codes


def test_reference_coverage_is_reported() -> None:
    # 冻结了 6 张，但只引用到参考图四
    report = check_video_prompt(COMPLIANT, duration_seconds=5, reference_count=6)
    codes = {problem.code for problem in report.problems}
    assert "PROMPT_REFERENCE_COVERAGE" in codes


def test_no_subtitle_clause_is_required() -> None:
    text = COMPLIANT.replace("无字幕，避免生成任何文字或字幕。", "")
    report = check_video_prompt(text, duration_seconds=5, reference_count=4)
    codes = {problem.code for problem in report.problems}
    assert "PROMPT_MISSING_NO_SUBTITLE" in codes


def test_forbidden_phrases_are_rejected() -> None:
    """手册 §6.2：m/s 严禁「连续继承」「保持一致」，及「同上」「如上」这类省略指代。"""
    for phrase in ("保持一致", "连续继承", "同上", "如上"):
        text = COMPLIANT + f"\n全片与参考图{phrase}。"
        report = check_video_prompt(text, duration_seconds=5, reference_count=4)
        codes = {problem.code for problem in report.problems}
        assert "PROMPT_FORBIDDEN_PHRASE" in codes, phrase


def test_legacy_terms_are_rejected() -> None:
    """手册 §5：旧代体系术语禁止出现在任何交付物中。"""
    text = COMPLIANT + "\nloRA 强度 0.8，ControlNet 控制姿态，负向提示词略。"
    report = check_video_prompt(text, duration_seconds=5, reference_count=4)
    codes = {problem.code for problem in report.problems}
    assert "PROMPT_LEGACY_TERM" in codes


def test_empty_prompt_is_an_error() -> None:
    report = check_video_prompt("", duration_seconds=5, reference_count=1)
    assert not report.ok
    assert report.problems[0].code == "PROMPT_EMPTY"
