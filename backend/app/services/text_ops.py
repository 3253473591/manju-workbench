"""文本类生成任务的编排：大纲、人物设定、剧本、分镜拆分、Clip 优化、资产规划。

每个 handler 的职责是「拼输入 → 调模型 → 落业务数据」；模型调用与 attempt /
usage / delivery 的记录统一走 generation_service。
"""

from __future__ import annotations

import json

from sqlmodel import Session

from app.core.errors import AppError
from app.core.utils import to_iso, utcnow
from app.models.canvas import CanvasNode
from app.models.content import Clip, ClipShot, ContentDocument
from app.models.enums import (
    AttemptTarget,
    DocumentKind,
    DocumentStatus,
    LogicalStatus,
    Operation,
    RevisionSource,
)
from app.models.generation import GenerationSubmission
from app.services import canvas_service, content_service, generation_service, naming

# --- 公共工具 -----------------------------------------------------------


def _requirement(submission: GenerationSubmission) -> str:
    payload = generation_service._payload(submission)
    return str(payload.get("requirement") or "")


def _count(submission: GenerationSubmission) -> int:
    payload = generation_service._payload(submission)
    try:
        return max(1, min(int(payload.get("count") or 1), 8))
    except (TypeError, ValueError):
        return 1


def _model_config_id(submission: GenerationSubmission) -> str | None:
    payload = generation_service._payload(submission)
    value = payload.get("modelConfigId")
    return str(value) if value else None


def _current_body(document: ContentDocument) -> str:
    return document.body or ""


def _node_for_document(db: Session, document: ContentDocument) -> CanvasNode:
    return canvas_service.ensure_text_node(db, document.episode_id, document)


def _sibling_document(
    db: Session,
    source: ContentDocument,
    *,
    title: str,
    body: str,
    created_by: str | None,
    metadata: dict | None = None,
    index: int = 0,
) -> tuple[ContentDocument, CanvasNode]:
    """优化分支：新建同级文档与节点，各从 v1（AI 生成）开始。"""
    document = ContentDocument(
        episode_id=source.episode_id,
        kind=source.kind,
        parent_document_id=source.id,
        title=title,
        body=body,
        status=DocumentStatus.saved,
        # 继承业务关联，但不复制历史版本
        bound_outline_revision_id=source.bound_outline_revision_id,
        bound_setting_revision_id=source.bound_setting_revision_id,
        source_revision_id=source.current_revision_id,
    )
    db.add(document)
    db.flush()
    content_service.append_revision(
        db,
        document,
        body=body,
        title=title,
        source=RevisionSource.ai,
        created_by=created_by,
        metadata=metadata,
    )

    source_node = _node_for_document(db, source)
    node = canvas_service.ensure_text_node(db, document.episode_id, document)
    canvas_service.place_node(
        db,
        node.id,
        anchor_node_id=source_node.id,
        direction="sibling",
        index=index,
        width=420.0,
        height=320.0,
    )
    node.label = title
    document.node_id = node.id
    db.add(node)
    db.add(document)
    db.flush()
    canvas_service.create_edge(
        db,
        document.episode_id,
        {
            "fromNodeId": source_node.id,
            "toNodeId": node.id,
            "edgeType": "derives",
            "note": "优化分支",
        },
    )
    return document, node


def _require_saved(document: ContentDocument, message: str) -> str:
    if not document.current_revision_id:
        raise AppError("BUSINESS_RULE_VIOLATION", message)
    return document.body


# --- 大纲 ---------------------------------------------------------------


def handle_outline_optimize(db: Session, submission: GenerationSubmission) -> None:
    document = content_service.get_document_or_404(db, submission.target_id or "")
    _require_saved(document, "请先保存大纲，再发送优化")
    requirement = _requirement(submission)
    count = _count(submission)

    produced: list[str] = []
    for index in range(count):
        user_prompt = (
            f"【大纲正文】\n{_current_body(document)}\n\n【本次需求】\n{requirement or '优化并扩写'}"
        )
        payload, attempt = generation_service.call_text(
            db,
            submission,
            system_key="outline_optimize",
            user_prompt=user_prompt,
            expect="text",
            input_snapshot={
                "sourceDocumentId": document.id,
                "sourceRevisionId": document.current_revision_id,
                "requirement": requirement,
                "index": index,
            },
            context={"title": document.title, "salt": index},
            target_type=AttemptTarget.document,
            target_id=document.id,
            model_config_id=_model_config_id(submission),
        )
        branch, node = _sibling_document(
            db,
            document,
            title=str(payload.get("title") or f"{document.title}（优化 {index + 1}）"),
            body=str(payload.get("body") or ""),
            created_by=submission.created_by,
            metadata={"operation": submission.operation.value, "attemptId": attempt.id},
            index=index,
        )
        generation_service.mark_delivery_verified(db, attempt)
        produced.append(node.id)

    generation_service.update_payload(submission, producedNodeIds=produced)
    db.commit()


def handle_setting_generate(db: Session, submission: GenerationSubmission) -> None:
    """人物设定先进入草稿区，不改写旧版本。"""
    outline = content_service.get_document_or_404(db, submission.target_id or "")
    if outline.kind != DocumentKind.outline:
        raise AppError("BUSINESS_RULE_VIOLATION", "人物设定只能从大纲文档生成")
    _require_saved(outline, "请先保存大纲，再生成人物设定")

    setting = content_service.find_setting_document(db, outline.episode_id)
    if setting is None:
        setting = content_service.create_document(
            db,
            episode_id=outline.episode_id,
            kind=DocumentKind.setting,
            title="人物设定",
            parent_document_id=outline.id,
        )
        setting.bound_outline_revision_id = outline.current_revision_id

    requirement = _requirement(submission)
    payload, attempt = generation_service.call_text(
        db,
        submission,
        system_key="setting_generate",
        user_prompt=f"【大纲正文】\n{outline.body}\n\n【本次需求】\n{requirement or '按大纲产出人物设定'}",
        expect="setting",
        input_snapshot={
            "outlineDocumentId": outline.id,
            "outlineRevisionId": outline.current_revision_id,
            "requirement": requirement,
        },
        context={"subject": (_episode_character_names(db, outline.episode_id) or [""])[0]},
        target_type=AttemptTarget.document,
        target_id=outline.id,
        model_config_id=_model_config_id(submission),
    )

    characters = payload.get("characters") or []
    if not isinstance(characters, list) or not characters:
        raise AppError("PROVIDER_REJECTED", "模型没有返回人物设定内容")

    setting.body = _render_setting(characters)
    setting.status = DocumentStatus.draft
    setting.bound_outline_revision_id = outline.current_revision_id
    db.add(setting)
    _sync_character_profiles(db, outline, setting, characters)
    generation_service.mark_delivery_verified(db, attempt)
    generation_service.update_payload(
        submission,
        producedNodeIds=[outline.node_id] if outline.node_id else [],
        result={"characters": characters, "settingDocumentId": setting.id},
    )
    db.commit()


def _sync_character_profiles(
    db: Session, outline: ContentDocument, setting: ContentDocument, characters: list[dict]
) -> None:
    """把设定落成结构化投影，便于后续资产规划与查重按字段读取。"""
    from sqlmodel import select

    from app.models.content import CharacterProfile

    for entry in characters:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        profile = db.exec(
            select(CharacterProfile).where(
                CharacterProfile.episode_id == outline.episode_id,
                CharacterProfile.name == name,
            )
        ).first()
        if profile is None:
            from app.models.project import Episode

            episode = db.get(Episode, outline.episode_id)
            profile = CharacterProfile(
                project_id=episode.project_id if episode else "",
                episode_id=outline.episode_id,
                document_id=setting.id,
                name=name,
            )
        profile.document_id = setting.id
        profile.identity = str(entry.get("identity") or "")
        profile.personality = str(entry.get("personality") or "")
        profile.appearance = str(entry.get("appearance") or "")
        profile.hair = str(entry.get("hair") or "")
        profile.base_outfit = str(entry.get("baseOutfit") or "")
        profile.outfit_variants = str(entry.get("outfitVariants") or "")
        profile.signature_props = str(entry.get("signatureProps") or "")
        db.add(profile)
    db.flush()


def _episode_character_names(db: Session, episode_id: str) -> list[str]:
    """从人物设定投影里取角色名。

    这才是角色名的**权威来源**：不要去猜正文里哪一行像人名——正文里可能有标题、旁白或台词，
    用「第一个短行」当人名会得到「你终于回来了。」这种荒唐结果。
    """
    from sqlmodel import select

    from app.models.content import CharacterProfile

    rows = db.exec(
        select(CharacterProfile)
        .where(CharacterProfile.episode_id == episode_id)
        .order_by(CharacterProfile.created_at)
    ).all()
    return [row.name for row in rows if row.name]


def _render_setting(characters: list[dict]) -> str:
    lines: list[str] = []
    for index, character in enumerate(characters, start=1):
        lines.append(f"## {index}. {character.get('name', '未命名')}")
        for label, key in (
            ("身份", "identity"),
            ("性格", "personality"),
            ("外貌特征", "appearance"),
            ("发型与头饰", "hair"),
            ("基础服装", "baseOutfit"),
            ("服装变体", "outfitVariants"),
            ("标志性随身道具", "signatureProps"),
        ):
            value = character.get(key)
            if value:
                lines.append(f"- {label}：{value}")
        lines.append("")
    return "\n".join(lines).strip()


# --- 剧本 ---------------------------------------------------------------


def handle_script_generate(db: Session, submission: GenerationSubmission) -> None:
    """大纲【生成剧本】：使用保存后的大纲与已确认设定，记录两者来源。"""
    outline = content_service.get_document_or_404(db, submission.target_id or "")
    if outline.kind != DocumentKind.outline:
        raise AppError("BUSINESS_RULE_VIOLATION", "剧本只能从大纲文档生成")
    _require_saved(outline, "请先保存大纲，再生成剧本")

    setting = content_service.find_setting_document(db, outline.episode_id)
    setting_text = (setting.body if setting else "") or ""
    if not setting_text.strip():
        raise AppError(
            "BUSINESS_RULE_VIOLATION",
            "人物设定为空，不能继续生成剧本",
            details={"outlineDocumentId": outline.id},
        )

    # 设定与大纲配套保存：设定草稿在这里一并落成不可变版本
    if setting is not None and setting.body.strip():
        current = (
            db.get(content_service.ContentRevision, setting.current_revision_id)
            if setting.current_revision_id
            else None
        )
        if current is None or current.body != setting.body:
            content_service.append_revision(
                db,
                setting,
                body=setting.body,
                title=setting.title,
                source=RevisionSource.ai,
                created_by=submission.created_by,
                metadata={"outlineRevisionId": outline.current_revision_id},
            )
        setting.bound_outline_revision_id = outline.current_revision_id
        db.add(setting)

    requirement = _requirement(submission)
    payload, attempt = generation_service.call_text(
        db,
        submission,
        system_key="script_generate",
        user_prompt=(
            f"【大纲正文】\n{outline.body}\n\n【人物设定】\n{setting_text}\n\n"
            f"【本次需求】\n{requirement or '写一集剧本'}"
        ),
        expect="text",
        input_snapshot={
            "outlineDocumentId": outline.id,
            "outlineRevisionId": outline.current_revision_id,
            "settingDocumentId": setting.id if setting else None,
            "settingRevisionId": setting.current_revision_id if setting else None,
            "requirement": requirement,
        },
        context={"title": "第 1 集剧本"},
        target_type=AttemptTarget.document,
        target_id=outline.id,
        model_config_id=_model_config_id(submission),
    )

    script = content_service.create_document(
        db,
        episode_id=outline.episode_id,
        kind=DocumentKind.script,
        title=str(payload.get("title") or "第 1 集剧本"),
        parent_document_id=None,
    )
    script.bound_outline_revision_id = outline.current_revision_id
    script.bound_setting_revision_id = setting.current_revision_id if setting else None
    script.source_revision_id = outline.current_revision_id
    body = str(payload.get("body") or "")

    outline_node = canvas_service.ensure_text_node(db, outline.episode_id, outline)
    node = canvas_service.ensure_text_node(db, outline.episode_id, script)
    canvas_service.place_node(
        db,
        node.id,
        anchor_node_id=outline_node.id,
        direction="downstream",
        width=460.0,
        height=340.0,
    )
    node.label = script.title
    script.node_id = node.id
    db.add(node)
    db.add(script)
    db.flush()

    content_service.append_revision(
        db,
        script,
        body=body,
        title=script.title,
        source=RevisionSource.ai,
        created_by=submission.created_by,
        metadata={"outlineRevisionId": outline.current_revision_id, "attemptId": attempt.id},
    )
    canvas_service.create_edge(
        db,
        script.episode_id,
        {
            "fromNodeId": outline_node.id,
            "toNodeId": node.id,
            "edgeType": "derives",
            "note": "生成剧本",
        },
    )
    generation_service.mark_delivery_verified(db, attempt)
    generation_service.update_payload(submission, producedNodeIds=[node.id])
    db.commit()


def handle_script_optimize(db: Session, submission: GenerationSubmission) -> None:
    document = content_service.get_document_or_404(db, submission.target_id or "")
    _require_saved(document, "请先保存剧本，再发送优化")
    requirement = _requirement(submission)
    count = _count(submission)

    produced: list[str] = []
    for index in range(count):
        payload, attempt = generation_service.call_text(
            db,
            submission,
            system_key="script_optimize",
            user_prompt=(
                f"【剧本正文】\n{document.body}\n\n【本次需求】\n{requirement or '优化当前剧本'}"
            ),
            expect="text",
            input_snapshot={
                "sourceDocumentId": document.id,
                "sourceRevisionId": document.current_revision_id,
                "settingRevisionId": document.bound_setting_revision_id,
                "requirement": requirement,
                "index": index,
            },
            context={"title": document.title, "salt": index},
            target_type=AttemptTarget.document,
            target_id=document.id,
            model_config_id=_model_config_id(submission),
        )
        _, node = _sibling_document(
            db,
            document,
            title=str(payload.get("title") or f"{document.title}（优化 {index + 1}）"),
            body=str(payload.get("body") or ""),
            created_by=submission.created_by,
            metadata={"operation": submission.operation.value, "attemptId": attempt.id},
            index=index,
        )
        generation_service.mark_delivery_verified(db, attempt)
        produced.append(node.id)

    generation_service.update_payload(submission, producedNodeIds=produced)
    db.commit()


def handle_script_split_clips(db: Session, submission: GenerationSubmission) -> None:
    """剧本【分镜】：直接拆为独立 Clip 节点，不先确认整份分镜表。"""
    script = content_service.get_document_or_404(db, submission.target_id or "")
    if script.kind != DocumentKind.script:
        raise AppError("BUSINESS_RULE_VIOLATION", "分镜只能从剧本文档拆分")
    _require_saved(script, "请先保存剧本，再拆分分镜")

    payload, attempt = generation_service.call_text(
        db,
        submission,
        system_key="script_split_clips",
        user_prompt=(
            f"【剧本正文】\n{script.body}\n\n【人物设定】\n"
            f"{(content_service.find_setting_document(db, script.episode_id).body if content_service.find_setting_document(db, script.episode_id) else '')}"
        ),
        expect="clips",
        input_snapshot={
            "scriptDocumentId": script.id,
            "scriptRevisionId": script.current_revision_id,
            "settingRevisionId": script.bound_setting_revision_id,
        },
        context={
            "clipCount": 3,
            "location": "雨夜校门",
            # 角色名来自设定投影；没有设定时留空，让模型/模板自己决定
            "subject": (_episode_character_names(db, script.episode_id) or [""])[0],
        },
        target_type=AttemptTarget.document,
        target_id=script.id,
        model_config_id=_model_config_id(submission),
    )

    clips_payload = payload.get("clips") or []
    if not isinstance(clips_payload, list) or not clips_payload:
        raise AppError("PROVIDER_REJECTED", "模型没有返回可用的 Clip 结构")

    script_node = canvas_service.ensure_text_node(db, script.episode_id, script)
    produced: list[str] = []
    existing_max = _max_clip_no(db, script.episode_id)

    for offset, entry in enumerate(clips_payload):
        clip_no = int(entry.get("clipNo") or (existing_max + offset + 1))
        clip = Clip(
            episode_id=script.episode_id,
            document_id=None,
            source_revision_id=script.current_revision_id,
            clip_no=clip_no,
            title=str(entry.get("title") or f"CLIP {clip_no:02d}"),
            duration_ms=int(entry.get("durationMs") or 5000),
            characters_json=json.dumps(entry.get("characters") or [], ensure_ascii=False),
            location_name=str(entry.get("locationName") or ""),
            status="ready",
            body=_render_clip_body(entry),
        )
        db.add(clip)
        db.flush()

        for shot_index, shot in enumerate(entry.get("shots") or []):
            db.add(
                ClipShot(
                    clip_id=clip.id,
                    shot_no=int(shot.get("shotNo") or shot_index + 1),
                    start_ms=int(shot.get("startMs") or 0),
                    end_ms=int(shot.get("endMs") or 0),
                    visual_text=str(shot.get("visualText") or ""),
                    camera_text=str(shot.get("cameraText") or ""),
                    dialogue_text=str(shot.get("dialogueText") or ""),
                    sound_text=str(shot.get("soundText") or ""),
                )
            )

        node = canvas_service.ensure_shot_node(db, script.episode_id, clip)
        canvas_service.place_node(
            db,
            node.id,
            anchor_node_id=script_node.id,
            direction="downstream",
            index=offset,
            width=320.0,
            height=240.0,
        )
        canvas_service.create_edge(
            db,
            script.episode_id,
            {
                "fromNodeId": script_node.id,
                "toNodeId": node.id,
                "edgeType": "derives",
                "note": "分镜拆分",
            },
        )
        produced.append(node.id)

    generation_service.mark_delivery_verified(db, attempt)
    generation_service.update_payload(submission, producedNodeIds=produced)
    db.commit()


def _render_clip_body(entry: dict) -> str:
    lines = [f"场景：{entry.get('locationName') or ''}"]
    for shot in entry.get("shots") or []:
        lines.append(
            f"镜头 {shot.get('shotNo')}"
            f"（{int(shot.get('startMs') or 0) / 1000:.1f}-{int(shot.get('endMs') or 0) / 1000:.1f}秒）"
        )
        lines.append(f"  画面与动作：{shot.get('visualText') or ''}")
        lines.append(f"  景别与运镜：{shot.get('cameraText') or ''}")
        lines.append(f"  对白与音效：{shot.get('dialogueText') or ''} / {shot.get('soundText') or ''}")
    return "\n".join(lines)


def _max_clip_no(db: Session, episode_id: str) -> int:
    from sqlmodel import select

    rows = db.exec(select(Clip.clip_no).where(Clip.episode_id == episode_id)).all()
    return max([int(r) for r in rows], default=0)


def handle_clip_optimize(db: Session, submission: GenerationSubmission) -> None:
    """Clip【发送】：优化当前 Clip 并创建同级候选。"""
    from app.services import clip_service

    clip = clip_service.get_clip_or_404(db, submission.target_id or "")
    requirement = _requirement(submission)
    count = _count(submission)

    from app.models.canvas import CanvasNode
    from sqlmodel import select

    source_node = db.exec(
        select(CanvasNode).where(CanvasNode.clip_id == clip.id, CanvasNode.deleted_at.is_(None))
    ).first()

    produced: list[str] = []
    for index in range(count):
        payload, attempt = generation_service.call_text(
            db,
            submission,
            system_key="clip_optimize",
            user_prompt=(
                f"【Clip 正文】\n{clip.body}\n\n【本次需求】\n{requirement or '优化这个 Clip'}"
            ),
            expect="clip_optimize",
            input_snapshot={
                "clipId": clip.id,
                "clipVersion": clip.version,
                "requirement": requirement,
                "index": index,
            },
            context={"title": clip.title, "salt": index},
            target_type=AttemptTarget.clip,
            target_id=clip.id,
            model_config_id=_model_config_id(submission),
        )

        body = str(payload.get("body") or clip.body)
        candidate = Clip(
            episode_id=clip.episode_id,
            source_revision_id=clip.source_revision_id,
            clip_no=clip.clip_no,
            title=str(payload.get("title") or clip.title),
            duration_ms=clip.duration_ms,
            characters_json=clip.characters_json,
            location_name=clip.location_name,
            status="ready",
            body=body,
        )
        db.add(candidate)
        db.flush()
        node = canvas_service.ensure_shot_node(db, clip.episode_id, candidate)
        canvas_service.place_node(
            db,
            node.id,
            anchor_node_id=source_node.id if source_node else None,
            direction="sibling",
            index=index,
            width=320.0,
            height=240.0,
        )
        if source_node is not None:
            canvas_service.create_edge(
                db,
                clip.episode_id,
                {
                    "fromNodeId": source_node.id,
                    "toNodeId": node.id,
                    "edgeType": "derives",
                    "note": "Clip 优化候选",
                },
            )
        generation_service.mark_delivery_verified(db, attempt)
        produced.append(node.id)

    generation_service.update_payload(submission, producedNodeIds=produced)
    db.commit()


# --- 资产规划 -----------------------------------------------------------


def _plan_assets(db: Session, submission: GenerationSubmission, *, asset_type: str, key: str) -> None:
    """三个入口共用：产出规范资产清单进入确认流程，不直接生图。"""
    from app.services import clip_service

    if asset_type == "character":
        script = content_service.get_document_or_404(db, submission.target_id or "")
        if script.kind != DocumentKind.script:
            raise AppError("BUSINESS_RULE_VIOLATION", "人物立绘只能从剧本规划")
        _require_saved(script, "请先保存剧本，再规划人物立绘")
        clips = [
            c
            for c in clip_service.list_clips(db, script.episode_id)
            if not _clip_hidden(db, c.id)
        ]
        characters = sorted(
            {
                *_episode_character_names(db, script.episode_id),
                *(name for c in clips for name in _clip_characters(c)),
            }
        )
        location = ""
        clip_ids = [c.id for c in clips]
    else:
        clip = clip_service.get_clip_or_404(db, submission.target_id or "")
        characters = sorted(_clip_characters(clip))
        location = clip.location_name
        clip_ids = [clip.id]

    payload, attempt = generation_service.call_text(
        db,
        submission,
        system_key=key,
        user_prompt=(
            f"【人物】{', '.join(characters) or '（无）'}\n【场景】{location or '（未指定）'}\n"
            f"【Clip 数量】{len(clip_ids)}"
        ),
        expect="assets",
        input_snapshot={
            "assetType": asset_type,
            "clipIds": clip_ids,
            "characters": characters,
            "location": location,
        },
        context={
            "assetType": asset_type,
            "clipIds": clip_ids,
            "subject": characters[0] if characters else "林晚",
            "location": location,
        },
        target_type=AttemptTarget.clip if asset_type != "character" else AttemptTarget.document,
        target_id=submission.target_id,
        model_config_id=_model_config_id(submission),
    )

    assets = payload.get("assets") or []
    if not isinstance(assets, list):
        raise AppError("PROVIDER_REJECTED", "模型返回的资产清单结构非法")

    # 落库前先归一：真实模型会用中文或自创英文（曾观测到 assetType="人物"、
    # imageKind="角色立绘"），也会给出 clip_001 这类不存在的 Clip id。
    # 词表归一 + 用权威 clipIds 覆盖，才能保证确认环节拿到的是一份干净清单。
    normalized: list[dict] = []
    for entry in assets:
        if not isinstance(entry, dict):
            continue
        asset_type = naming.parse_asset_type(entry.get("assetType"))
        if asset_type is None:
            raise AppError(
                "PROVIDER_REJECTED",
                f"模型返回了无法识别的资产类型：{entry.get('assetType')!r}",
                details={"rawValue": str(entry.get("assetType"))},
            )
        normalized.append(
            {
                "assetType": asset_type.value,
                "canonicalName": str(entry.get("canonicalName") or "").strip(),
                "variantName": str(entry.get("variantName") or "").strip(),
                "imageKind": naming.parse_image_kind(str(entry.get("imageKind") or "")),
                "clipIds": clip_ids,
            }
        )

    generation_service.mark_delivery_verified(db, attempt)
    generation_service.update_payload(
        submission,
        result={"assets": normalized, "assetType": asset_type, "clipIds": clip_ids},
        producedNodeIds=[],
    )
    db.commit()


def _clip_characters(clip: Clip) -> list[str]:
    try:
        value = json.loads(clip.characters_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value if item]


def _clip_hidden(db: Session, clip_id: str) -> bool:
    from sqlmodel import select

    node = db.exec(
        select(CanvasNode).where(CanvasNode.clip_id == clip_id, CanvasNode.deleted_at.is_(None))
    ).first()
    return bool(node and node.hidden)


def handle_plan_characters(db: Session, submission: GenerationSubmission) -> None:
    _plan_assets(db, submission, asset_type="character", key="plan_characters")


def handle_plan_scenes(db: Session, submission: GenerationSubmission) -> None:
    _plan_assets(db, submission, asset_type="scene", key="plan_scenes")


def handle_plan_props(db: Session, submission: GenerationSubmission) -> None:
    _plan_assets(db, submission, asset_type="prop", key="plan_props")


HANDLERS = {
    Operation.outline_optimize: handle_outline_optimize,
    Operation.setting_generate: handle_setting_generate,
    Operation.script_generate: handle_script_generate,
    Operation.script_optimize: handle_script_optimize,
    Operation.script_split_clips: handle_script_split_clips,
    Operation.clip_optimize: handle_clip_optimize,
    Operation.plan_characters: handle_plan_characters,
    Operation.plan_scenes: handle_plan_scenes,
    Operation.plan_props: handle_plan_props,
}

__all__ = ["HANDLERS"]
