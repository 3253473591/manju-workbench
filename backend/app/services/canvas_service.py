"""Canvas 服务：节点、布局、依赖边。

关键边界（domain-boundaries §4）：
- 拖动与改尺寸只写 `canvas_node_layout`，不产生内容版本，也不影响内容指纹。
- 依赖边只用于追溯，不自动重跑、不覆盖产物、不自动拼进模型输入。
"""

from __future__ import annotations

import hashlib
import json

from sqlmodel import Session, select

from app.core.errors import AppError
from app.core.utils import to_iso, utcnow
from app.models.canvas import CanvasNode, CanvasNodeLayout, DependencyEdge
from app.models.common import touch
from app.models.enums import CanvasNodeType, EdgeType

#: 阶段顺序：文本 → 分镜(Clip) → 图片 → 视频。拖线只允许同级或下一阶段。
STAGE_RANK: dict[CanvasNodeType, int] = {
    CanvasNodeType.text: 0,
    CanvasNodeType.shot: 1,
    CanvasNodeType.image: 2,
    CanvasNodeType.video: 3,
}

DEFAULT_WIDTH = 320.0
DEFAULT_HEIGHT = 220.0

MIN_WIDTH = 120.0
MAX_WIDTH = 2000.0
MIN_HEIGHT = 80.0
MAX_HEIGHT = 4000.0


# --- 序列化 -------------------------------------------------------------


def layout_out(layout: CanvasNodeLayout | None) -> dict | None:
    if layout is None:
        return None
    return {
        "x": layout.x,
        "y": layout.y,
        "width": layout.width,
        "height": layout.height,
        "zIndex": layout.z_index,
    }


def node_out(node: CanvasNode) -> dict:
    return {
        "id": node.id,
        "episodeId": node.episode_id,
        "nodeType": node.node_type.value,
        "documentId": node.document_id,
        "clipId": node.clip_id,
        "mediaId": node.media_id,
        "parentNodeId": node.parent_node_id,
        "label": node.label,
        "hidden": node.hidden,
        "contentFingerprint": node.content_fingerprint,
        "createdAt": to_iso(node.created_at),
        "updatedAt": to_iso(node.updated_at),
    }


def edge_out(edge: DependencyEdge) -> dict:
    return {
        "id": edge.id,
        "episodeId": edge.episode_id,
        "fromNodeId": edge.from_node_id,
        "toNodeId": edge.to_node_id,
        "edgeType": edge.edge_type.value,
        "note": edge.note,
    }


# --- 查询 ---------------------------------------------------------------


def get_node_or_404(db: Session, node_id: str) -> CanvasNode:
    node = db.get(CanvasNode, node_id)
    if node is None or node.deleted_at is not None:
        raise AppError("RESOURCE_NOT_FOUND", "画布节点不存在或已删除")
    return node


def get_layout(db: Session, node_id: str) -> CanvasNodeLayout | None:
    return db.exec(
        select(CanvasNodeLayout).where(CanvasNodeLayout.node_id == node_id)
    ).first()


def find_media_node(db: Session, media_id: str) -> CanvasNode | None:
    return db.exec(
        select(CanvasNode).where(
            CanvasNode.media_id == media_id, CanvasNode.deleted_at.is_(None)
        )
    ).first()


def find_clip_node(db: Session, clip_id: str) -> CanvasNode | None:
    return db.exec(
        select(CanvasNode).where(
            CanvasNode.clip_id == clip_id, CanvasNode.deleted_at.is_(None)
        )
    ).first()


def get_canvas(db: Session, episode_id: str) -> dict:
    nodes = db.exec(
        select(CanvasNode)
        .where(CanvasNode.episode_id == episode_id, CanvasNode.deleted_at.is_(None))
        .order_by(CanvasNode.created_at)
    ).all()
    node_ids = [n.id for n in nodes]
    layouts: dict[str, CanvasNodeLayout] = {}
    if node_ids:
        rows = db.exec(
            select(CanvasNodeLayout).where(CanvasNodeLayout.node_id.in_(node_ids))
        ).all()
        layouts = {row.node_id: row for row in rows}

    edges = db.exec(
        select(DependencyEdge)
        .where(DependencyEdge.episode_id == episode_id)
        .order_by(DependencyEdge.created_at)
    ).all()

    items = []
    for node in nodes:
        layout = layouts.get(node.id)
        items.append(
            {
                "node": node_out(node),
                "layout": layout_out(layout)
                or {
                    "x": 0.0,
                    "y": 0.0,
                    "width": DEFAULT_WIDTH,
                    "height": DEFAULT_HEIGHT,
                    "zIndex": 0,
                },
            }
        )
    return {
        "episodeId": episode_id,
        "nodes": items,
        "edges": [edge_out(e) for e in edges],
    }


def get_node_detail(db: Session, node_id: str) -> dict:
    node = get_node_or_404(db, node_id)
    detail: dict = {
        "node": node_out(node),
        "layout": layout_out(get_layout(db, node.id)),
        "document": None,
        "clip": None,
        "mediaNode": None,
    }
    if node.node_type == CanvasNodeType.text and node.document_id:
        from app.services import content_service

        detail["document"] = content_service.get_document_detail(db, node.document_id)
    elif node.node_type == CanvasNodeType.shot and node.clip_id:
        from app.services import clip_service

        detail["clip"] = clip_service.get_clip_detail(db, node.clip_id)
    elif node.media_id:
        from app.services import media_service

        detail["mediaNode"] = media_service.get_media_node_detail(db, node.media_id)
    return detail


# --- 写入 ---------------------------------------------------------------


def _validate_refs(payload: dict) -> CanvasNodeType:
    try:
        node_type = CanvasNodeType(payload["nodeType"])
    except (KeyError, ValueError) as exc:
        raise AppError("NODE_TYPE_INVALID", "节点类型非法") from exc

    required = {
        CanvasNodeType.text: "documentId",
        CanvasNodeType.shot: "clipId",
        CanvasNodeType.image: "mediaId",
        CanvasNodeType.video: "mediaId",
    }[node_type]
    if not payload.get(required):
        raise AppError(
            "NODE_TYPE_INVALID",
            f"{node_type.value} 节点必须提供 {required}",
            details={"nodeType": node_type.value, "required": required},
        )
    return node_type


def create_node(db: Session, episode_id: str, payload: dict) -> dict:
    node_type = _validate_refs(payload)
    layout_input = payload.get("layout") or {}
    _validate_layout(layout_input, partial=True)

    node = CanvasNode(
        episode_id=episode_id,
        node_type=node_type,
        document_id=payload.get("documentId"),
        clip_id=payload.get("clipId"),
        media_id=payload.get("mediaId"),
        parent_node_id=payload.get("parentNodeId"),
        label=payload.get("label") or "",
    )
    db.add(node)
    db.flush()
    _ensure_layout(db, node.id, layout_input)
    db.commit()
    db.refresh(node)
    return {"node": node_out(node)}


def _validate_layout(payload: dict, *, partial: bool) -> None:
    if partial and not payload:
        return
    width = payload.get("width")
    height = payload.get("height")
    if width is not None and not (MIN_WIDTH <= width <= MAX_WIDTH):
        raise AppError(
            "CANVAS_LAYOUT_INVALID",
            f"节点宽度必须在 {MIN_WIDTH:.0f}–{MAX_WIDTH:.0f} 之间",
            details={"minWidth": MIN_WIDTH, "maxWidth": MAX_WIDTH},
        )
    if height is not None and not (MIN_HEIGHT <= height <= MAX_HEIGHT):
        raise AppError(
            "CANVAS_LAYOUT_INVALID",
            f"节点高度必须在 {MIN_HEIGHT:.0f}–{MAX_HEIGHT:.0f} 之间",
            details={"minHeight": MIN_HEIGHT, "maxHeight": MAX_HEIGHT},
        )


def _ensure_layout(
    db: Session, node_id: str, payload: dict
) -> CanvasNodeLayout:
    layout = get_layout(db, node_id)
    if layout is None:
        layout = CanvasNodeLayout(
            node_id=node_id,
            x=float(payload.get("x", 0.0)),
            y=float(payload.get("y", 0.0)),
            width=float(payload.get("width", DEFAULT_WIDTH)),
            height=float(payload.get("height", DEFAULT_HEIGHT)),
            z_index=int(payload.get("zIndex", 0)),
        )
        db.add(layout)
        db.flush()
    return layout


def update_layout(db: Session, node_id: str, payload: dict) -> dict:
    node = get_node_or_404(db, node_id)
    _validate_layout(payload, partial=False)
    layout = _ensure_layout(db, node.id, payload)
    layout.x = float(payload["x"])
    layout.y = float(payload["y"])
    layout.width = float(payload["width"])
    layout.height = float(payload["height"])
    layout.z_index = int(payload.get("zIndex", layout.z_index))
    touch(layout)
    db.add(layout)
    # 布局变化不写内容版本、不重算内容指纹
    db.commit()
    db.refresh(layout)
    return {"nodeId": node.id, "layout": layout_out(layout)}


def update_node(db: Session, node_id: str, payload: dict) -> dict:
    node = get_node_or_404(db, node_id)
    if payload.get("label") is not None:
        node.label = str(payload["label"])[:200]
    touch(node)
    db.add(node)
    db.commit()
    db.refresh(node)
    return {"node": node_out(node)}


def set_visibility(db: Session, node_id: str, hidden: bool) -> dict:
    node = get_node_or_404(db, node_id)
    node.hidden = bool(hidden)
    touch(node)
    db.add(node)
    db.commit()
    db.refresh(node)
    return {"node": node_out(node)}


def delete_node(db: Session, node_id: str) -> None:
    """软删除节点；内容的不可变版本与媒体文件保留。"""
    node = get_node_or_404(db, node_id)
    node.deleted_at = utcnow()
    touch(node)
    db.add(node)
    db.commit()


def create_edge(
    db: Session, episode_id: str, payload: dict, *, enforce_stage_rule: bool = True
) -> dict:
    """建立依赖边。

    `enforce_stage_rule=True` 只用于**用户手动拖线**：同级或下一阶段，视频不手动同级拖线。
    专用按钮与自动抽卡产生的关系（Clip → 视频方案、方案 → 结果）不受该限制，
    调用方需要显式传 False（PRD §7）。
    """
    from_node = get_node_or_404(db, payload["fromNodeId"])
    to_node = get_node_or_404(db, payload["toNodeId"])
    if from_node.episode_id != episode_id or to_node.episode_id != episode_id:
        raise AppError("NODE_TYPE_INVALID", "依赖边两端的节点必须属于同一剧集")

    try:
        edge_type = EdgeType(payload.get("edgeType") or "derives")
    except ValueError as exc:
        raise AppError("NODE_TYPE_INVALID", "依赖边类型非法") from exc

    if enforce_stage_rule:
        _assert_edge_allowed(from_node.node_type, to_node.node_type)

    existing = db.exec(
        select(DependencyEdge).where(
            DependencyEdge.from_node_id == from_node.id,
            DependencyEdge.to_node_id == to_node.id,
            DependencyEdge.edge_type == edge_type,
        )
    ).first()
    if existing is not None:
        return {"edge": edge_out(existing)}

    edge = DependencyEdge(
        episode_id=episode_id,
        from_node_id=from_node.id,
        to_node_id=to_node.id,
        edge_type=edge_type,
        note=payload.get("note") or "",
    )
    db.add(edge)
    db.commit()
    db.refresh(edge)
    return {"edge": edge_out(edge)}


def _assert_edge_allowed(
    source: CanvasNodeType, target: CanvasNodeType
) -> None:
    """用户手动拖线只允许同级或下一阶段；视频不手动同级拖线。"""
    source_rank = STAGE_RANK[source]
    target_rank = STAGE_RANK[target]
    if target_rank == source_rank:
        if source == CanvasNodeType.video:
            raise AppError(
                "NODE_TYPE_INVALID",
                "视频节点不支持手动同级拖线，请从 Clip 重新生成视频方案",
            )
        return
    if target_rank == source_rank + 1:
        return
    raise AppError(
        "NODE_TYPE_INVALID",
        "拖线仅允许同级或下一阶段",
        details={"fromStage": source.value, "toStage": target.value},
    )


def delete_edge(db: Session, edge_id: str) -> None:
    edge = db.get(DependencyEdge, edge_id)
    if edge is None:
        raise AppError("RESOURCE_NOT_FOUND", "依赖边不存在")
    db.delete(edge)
    db.commit()


# --- 供其它服务调用的节点创建辅助 ---------------------------------------


def place_node(
    db: Session,
    node_id: str,
    *,
    anchor_node_id: str | None = None,
    direction: str = "sibling",
    index: int = 0,
    width: float = DEFAULT_WIDTH,
    height: float = DEFAULT_HEIGHT,
) -> CanvasNodeLayout:
    """为新节点落一个不压住旧节点的布局（已存在则更新，不重复插入）。"""
    node = db.get(CanvasNode, node_id)
    episode_id = node.episode_id if node else ""
    x, y = suggest_position(
        db,
        episode_id,
        anchor_node_id=anchor_node_id or node_id,
        direction=direction,
        index=index,
        width=width,
        height=height,
    )
    layout = get_layout(db, node_id)
    if layout is None:
        layout = CanvasNodeLayout(node_id=node_id)
    layout.x = x
    layout.y = y
    layout.width = width
    layout.height = height
    db.add(layout)
    db.flush()
    return layout


def ensure_text_node(db: Session, episode_id: str, document: object) -> CanvasNode:
    node = db.exec(
        select(CanvasNode).where(
            CanvasNode.document_id == document.id, CanvasNode.deleted_at.is_(None)
        )
    ).first()
    if node is not None:
        return node
    node = CanvasNode(
        episode_id=episode_id,
        node_type=CanvasNodeType.text,
        document_id=document.id,
        label=document.title or "",
    )
    db.add(node)
    db.flush()
    place_node(db, node.id, direction="sibling", width=420.0, height=320.0)
    return node


def ensure_shot_node(db: Session, episode_id: str, clip: object) -> CanvasNode:
    node = db.exec(
        select(CanvasNode).where(
            CanvasNode.clip_id == clip.id, CanvasNode.deleted_at.is_(None)
        )
    ).first()
    if node is not None:
        return node
    node = CanvasNode(
        episode_id=episode_id,
        node_type=CanvasNodeType.shot,
        clip_id=clip.id,
        label=clip.title or f"CLIP {clip.clip_no:02d}",
    )
    db.add(node)
    db.flush()
    place_node(db, node.id, direction="sibling", width=320.0, height=240.0)
    return node


def create_media_node(
    db: Session,
    episode_id: str,
    *,
    node_type: CanvasNodeType,
    media_id: str,
    label: str,
    parent_node_id: str | None,
    x: float,
    y: float,
    width: float,
    height: float,
) -> CanvasNode:
    node = CanvasNode(
        episode_id=episode_id,
        node_type=node_type,
        media_id=media_id,
        parent_node_id=parent_node_id,
        label=label,
    )
    db.add(node)
    db.flush()
    db.add(
        CanvasNodeLayout(
            node_id=node.id, x=x, y=y, width=width, height=height, z_index=0
        )
    )
    db.flush()
    return node


def compute_fingerprint(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- 新节点摆放 ---------------------------------------------------------

GAP_X = 56.0
GAP_Y = 40.0


def _occupied_rects(db: Session, episode_id: str) -> list[tuple[float, float, float, float]]:
    rows = db.exec(
        select(CanvasNodeLayout)
        .join(CanvasNode, CanvasNode.id == CanvasNodeLayout.node_id)
        .where(CanvasNode.episode_id == episode_id, CanvasNode.deleted_at.is_(None))
    ).all()
    return [(r.x, r.y, r.width, r.height) for r in rows]


def _collides(
    rect: tuple[float, float, float, float],
    others: list[tuple[float, float, float, float]],
) -> bool:
    x, y, width, height = rect
    for ox, oy, ow, oh in others:
        if x < ox + ow and ox < x + width and y < oy + oh and oy < y + height:
            return True
    return False


def suggest_position(
    db: Session,
    episode_id: str,
    *,
    anchor_node_id: str | None = None,
    direction: str = "sibling",
    index: int = 0,
    width: float = DEFAULT_WIDTH,
    height: float = DEFAULT_HEIGHT,
) -> tuple[float, float]:
    """为新节点找一个不压住旧节点的位置。

    整体是**从左到右的生产流水线**：下游阶段排在锚点右边，同级候选排在锚点右边并依次向下。
    与已有矩形重叠就继续下移，**不移动旧节点腾位置**（PRD §7）。

    早期版本把下游一律排到锚点下方，结果整条链路被排成 1000×2400 的竖长条，
    任何「适应画布」都会把节点缩到 0.25 倍、完全不可读。
    """
    occupied = _occupied_rects(db, episode_id)
    if anchor_node_id:
        anchor = get_layout(db, anchor_node_id)
        ax = anchor.x if anchor else 0.0
        ay = anchor.y if anchor else 0.0
        aw = anchor.width if anchor else DEFAULT_WIDTH
        ah = anchor.height if anchor else DEFAULT_HEIGHT
    else:
        ax = ay = 0.0
        aw, ah = DEFAULT_WIDTH, DEFAULT_HEIGHT

    # 下游阶段与同级候选都往右排；index 决定同一列里往下第几格
    x = ax + aw + GAP_X
    y = ay + index * (height + GAP_Y)

    guard = 0
    while _collides((x, y, width, height), occupied) and guard < 500:
        y += height + GAP_Y
        guard += 1
    return x, y
