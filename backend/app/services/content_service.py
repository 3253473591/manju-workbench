"""内容服务：草稿、不可变版本与业务绑定。

版本语义（PRD §4）：
- 草稿（document.body）与不可变历史版本（content_revisions）分开保存。
- 保存或生成前与选中版本比较，**实际修改才新增版本**；首次保存为 v1。
- 切换历史版本不新增版本。
- `document.version` 是乐观锁计数器，每次被接受的写入 +1。
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from app.core.errors import AppError
from app.core.utils import to_iso, utcnow
from app.models.common import touch
from app.models.content import ContentDocument, ContentRevision
from app.models.enums import DocumentKind, DocumentStatus, RevisionSource


def revision_out(revision: ContentRevision) -> dict:
    try:
        metadata = json.loads(revision.metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return {
        "id": revision.id,
        "documentId": revision.document_id,
        "revisionNo": revision.revision_no,
        "source": revision.source.value,
        "title": revision.title,
        "body": revision.body,
        "metadata": metadata,
        "createdBy": revision.created_by,
        "createdAt": to_iso(revision.created_at),
    }


def document_out(document: ContentDocument) -> dict:
    return {
        "id": document.id,
        "episodeId": document.episode_id,
        "kind": document.kind.value,
        "parentDocumentId": document.parent_document_id,
        "title": document.title,
        "body": document.body,
        "status": document.status.value,
        "version": document.version,
        "currentRevisionId": document.current_revision_id,
        "boundOutlineRevisionId": document.bound_outline_revision_id,
        "boundSettingRevisionId": document.bound_setting_revision_id,
        "sourceRevisionId": document.source_revision_id,
        "nodeId": document.node_id,
        "createdAt": to_iso(document.created_at),
        "updatedAt": to_iso(document.updated_at),
    }


def get_document_or_404(db: Session, document_id: str) -> ContentDocument:
    document = db.get(ContentDocument, document_id)
    if document is None or document.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "文档不存在或已删除")
    return document


def get_revision_or_404(db: Session, revision_id: str) -> ContentRevision:
    revision = db.get(ContentRevision, revision_id)
    if revision is None:
        raise AppError("RESOURCE_NOT_FOUND", "版本不存在")
    return revision


def list_revisions(db: Session, document_id: str) -> list[ContentRevision]:
    return list(
        db.exec(
            select(ContentRevision)
            .where(ContentRevision.document_id == document_id)
            .order_by(ContentRevision.revision_no.desc())
        ).all()
    )


def get_document_detail(db: Session, document_id: str) -> dict:
    document = get_document_or_404(db, document_id)
    revisions = list_revisions(db, document.id)
    current = None
    if document.current_revision_id:
        current = db.get(ContentRevision, document.current_revision_id)
    has_unsaved = bool(current and (current.body != document.body or current.title != document.title))
    if current is None and document.body.strip():
        # 还没有任何版本，但草稿里有内容 → 视为未保存
        has_unsaved = True
    return {
        "document": document_out(document),
        "currentRevision": revision_out(current) if current else None,
        "revisions": [revision_out(r) for r in revisions],
        "hasUnsavedDraft": has_unsaved,
    }


def _check_expected_version(document: ContentDocument, expected: int | None) -> None:
    if expected is None:
        return
    if expected != document.version:
        raise AppError(
            "CONTENT_VERSION_CONFLICT",
            "内容已被更新，请刷新后重试",
            details={"expectedVersion": expected, "actualVersion": document.version},
        )


def update_draft(db: Session, document_id: str, payload: dict) -> dict:
    document = get_document_or_404(db, document_id)
    _check_expected_version(document, payload.get("expectedVersion"))

    changed = False
    if payload.get("body") is not None and payload["body"] != document.body:
        document.body = payload["body"]
        changed = True
    if payload.get("title") is not None and payload["title"] != document.title:
        document.title = payload["title"][:200]
        changed = True

    if changed:
        # 草稿保存不产生不可变版本，但会推进乐观锁版本号
        document.version += 1
        touch(document)
        db.add(document)
        db.commit()
    return get_document_detail(db, document.id)


def create_revision(db: Session, document_id: str, payload: dict) -> tuple[dict, bool]:
    """返回 (详情, 是否新增版本)。未实际修改或切换历史版本时返回 False。"""
    document = get_document_or_404(db, document_id)
    _check_expected_version(document, payload.get("expectedVersion"))

    # 1) 切换历史版本：不新增版本，只把指针与草稿切过去
    from_revision_id = payload.get("fromRevisionId")
    if from_revision_id:
        revision = get_revision_or_404(db, from_revision_id)
        if revision.document_id != document.id:
            raise AppError("INVALID_REQUEST", "该版本不属于当前文档")
        document.current_revision_id = revision.id
        document.body = revision.body
        document.title = revision.title
        document.version += 1
        touch(document)
        db.add(document)
        db.commit()
        return get_document_detail(db, document.id), False

    body = payload.get("body")
    if body is None:
        body = document.body
    title = payload.get("title")
    if title is None:
        title = document.title

    current = (
        db.get(ContentRevision, document.current_revision_id)
        if document.current_revision_id
        else None
    )
    # 2) 与当前选中版本比较：没有实际修改就不新增版本
    if current is not None and current.body == body and current.title == title:
        document.body = body
        document.title = title or document.title
        db.add(document)
        db.commit()
        return get_document_detail(db, document.id), False

    source = payload.get("source")
    try:
        revision_source = RevisionSource(source) if source else RevisionSource.user
    except ValueError as exc:
        raise AppError("INVALID_REQUEST", "版本来源取值非法") from exc

    revision = append_revision(
        db,
        document,
        body=body,
        title=title,
        source=revision_source,
        created_by=payload.get("createdBy"),
    )
    db.commit()
    db.refresh(revision)
    return get_document_detail(db, document.id), True


def append_revision(
    db: Session,
    document: ContentDocument,
    *,
    body: str,
    title: str,
    source: RevisionSource,
    created_by: str | None = None,
    metadata: dict | None = None,
) -> ContentRevision:
    """写入一个不可变版本，并把它设为当前版本。调用方负责 commit。"""
    last_no = db.exec(
        select(ContentRevision.revision_no)
        .where(ContentRevision.document_id == document.id)
        .order_by(ContentRevision.revision_no.desc())
        .limit(1)
    ).first()
    next_no = int(last_no or 0) + 1

    revision = ContentRevision(
        document_id=document.id,
        revision_no=next_no,
        source=source,
        title=title or "",
        body=body or "",
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        created_by=created_by,
    )
    db.add(revision)
    db.flush()

    document.body = body or ""
    document.title = title or document.title
    document.current_revision_id = revision.id
    document.status = DocumentStatus.saved
    document.version += 1
    touch(document)
    db.add(document)
    db.flush()
    return revision


def save_revision(
    db: Session,
    document: ContentDocument,
    *,
    body: str,
    title: str | None = None,
    source: RevisionSource = RevisionSource.ai,
    created_by: str | None = None,
    metadata: dict | None = None,
) -> ContentRevision:
    """供生成服务调用：直接落一个不可变版本。"""
    revision = append_revision(
        db,
        document,
        body=body,
        title=title if title is not None else document.title,
        source=source,
        created_by=created_by,
        metadata=metadata,
    )
    db.commit()
    db.refresh(revision)
    return revision


def create_document(
    db: Session,
    *,
    episode_id: str,
    kind: DocumentKind,
    title: str,
    parent_document_id: str | None = None,
) -> ContentDocument:
    document = ContentDocument(
        episode_id=episode_id,
        kind=kind,
        title=title,
        parent_document_id=parent_document_id,
        body="",
        status=DocumentStatus.draft,
    )
    db.add(document)
    db.flush()
    return document


def find_outline_document(db: Session, episode_id: str) -> ContentDocument | None:
    return db.exec(
        select(ContentDocument).where(
            ContentDocument.episode_id == episode_id,
            ContentDocument.kind == DocumentKind.outline,
            ContentDocument.deleted_at.is_(None),
        )
    ).first()


def find_setting_document(db: Session, episode_id: str) -> ContentDocument | None:
    return db.exec(
        select(ContentDocument).where(
            ContentDocument.episode_id == episode_id,
            ContentDocument.kind == DocumentKind.setting,
            ContentDocument.deleted_at.is_(None),
        )
    ).first()


def encode_metadata(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)
