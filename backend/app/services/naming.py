"""资产命名规范解析。

规范来源：交接文档 §5.3。**完全匹配，不做语义匹配**；忽略扩展名。
不认识的命名一律返回 None，交由调用方决定，绝不猜。

    00人物-<名>[-<变体>]-<立绘|三视图|面部特写>.png  → 角色
    00兽设-<名>[-<角度>].png                        → 角色
    01场景-<名>[-<描述>].png                        → 场景
    02道具-<名>.png / 00载具-<名>.png / 03动物-<名>.png → 道具
    00参考-<镜号>-<名>.png                          → 不收（镜头锚点）
    _候选- / _废- / _旧- / _备份-                    → 默认跳过
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import AssetType

#: 前缀 → (资产类型, 默认图种)
PREFIX_RULES: dict[str, tuple[AssetType, str]] = {
    "00人物": (AssetType.character, ""),
    "00兽设": (AssetType.character, ""),
    "01场景": (AssetType.scene, "scene"),
    "02道具": (AssetType.prop, "prop"),
    "00载具": (AssetType.prop, "prop"),
    "03动物": (AssetType.prop, "prop"),
}

#: 图种中文名 → 内部 image_kind（同时收录模型可能自创的写法）
IMAGE_KIND_ALIASES: dict[str, str] = {
    "立绘": "portrait",
    "角色立绘": "portrait",
    "人物立绘": "portrait",
    "全身参考": "portrait",
    "full_body_reference": "portrait",
    "portrait": "portrait",
    "三视图": "three_view",
    "three_view": "three_view",
    "turnaround": "three_view",
    "面部特写": "face_closeup",
    "脸部特写": "face_closeup",
    "face_closeup": "face_closeup",
    "closeup": "face_closeup",
    "场景": "scene",
    "场景图": "scene",
    "scene": "scene",
    "道具": "prop",
    "道具图": "prop",
    "prop": "prop",
}

#: 资产类型别名。真实模型经常会用中文或自创英文，**绝不能直接拿去构造枚举**。
ASSET_TYPE_ALIASES: dict[str, AssetType] = {
    "character": AssetType.character,
    "characters": AssetType.character,
    "char": AssetType.character,
    "person": AssetType.character,
    "role": AssetType.character,
    "人物": AssetType.character,
    "角色": AssetType.character,
    "主角": AssetType.character,
    "配角": AssetType.character,
    "兽设": AssetType.character,
    "scene": AssetType.scene,
    "scenes": AssetType.scene,
    "location": AssetType.scene,
    "场景": AssetType.scene,
    "地点": AssetType.scene,
    "环境": AssetType.scene,
    "prop": AssetType.prop,
    "props": AssetType.prop,
    "item": AssetType.prop,
    "道具": AssetType.prop,
    "物品": AssetType.prop,
    "载具": AssetType.prop,
    "动物": AssetType.prop,
}

#: 这些前缀的素材不收录
SKIP_PREFIXES = ("_候选-", "_废-", "_旧-", "_备份-", "00参考-")

#: 不收录的图种（镜头锚点）
EXCLUDED_KIND = "reference"


@dataclass(frozen=True)
class ParsedAsset:
    filename: str
    asset_type: AssetType
    canonical_name: str
    variant_name: str
    image_kind: str


def strip_extension(filename: str) -> str:
    name = filename.replace("\\", "/").split("/")[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.strip()


def should_skip(filename: str) -> bool:
    stem = strip_extension(filename)
    return stem.startswith(SKIP_PREFIXES)


def parse_asset_filename(filename: str) -> ParsedAsset | None:
    """解析成功返回 ParsedAsset；命名不符合规范返回 None。"""
    stem = strip_extension(filename)
    if not stem or stem.startswith(SKIP_PREFIXES):
        return None

    for prefix, (asset_type, default_kind) in PREFIX_RULES.items():
        marker = f"{prefix}-"
        if not stem.startswith(marker):
            continue
        remainder = stem[len(marker) :]
        parts = [p for p in remainder.split("-") if p != ""]
        if not parts:
            return None

        image_kind = default_kind
        if len(parts) >= 2 and parts[-1] in IMAGE_KIND_ALIASES:
            image_kind = IMAGE_KIND_ALIASES[parts[-1]]
            parts = parts[:-1]
        if not parts:
            return None

        canonical_name = parts[0]
        variant_name = "-".join(parts[1:])
        return ParsedAsset(
            filename=filename,
            asset_type=asset_type,
            canonical_name=canonical_name,
            variant_name=variant_name,
            image_kind=image_kind,
        )
    return None


def parse_image_kind(value: str) -> str:
    """把用户/前端/模型传入的图种统一成内部取值；认不出就原样返回。"""
    if not value:
        return ""
    key = str(value).strip()
    return IMAGE_KIND_ALIASES.get(key) or IMAGE_KIND_ALIASES.get(key.lower(), key)


def parse_asset_type(value: object) -> AssetType | None:
    """把模型/用户给的资产类型归一；认不出返回 None，由调用方决定报错还是兜底。"""
    if value is None:
        return None
    if isinstance(value, AssetType):
        return value
    key = str(value).strip()
    if not key:
        return None
    if key in ASSET_TYPE_ALIASES:
        return ASSET_TYPE_ALIASES[key]
    return ASSET_TYPE_ALIASES.get(key.lower())


def build_filename(
    asset_type: AssetType, canonical_name: str, variant_name: str = "", image_kind: str = ""
) -> str:
    """反向拼回规范名，用于展示与查重提示。"""
    prefix = {
        AssetType.character: "00人物",
        AssetType.scene: "01场景",
        AssetType.prop: "02道具",
    }[asset_type]
    reverse_kind = {v: k for k, v in IMAGE_KIND_ALIASES.items()}
    parts = [prefix, canonical_name]
    if variant_name:
        parts.append(variant_name)
    if image_kind and image_kind in reverse_kind:
        parts.append(reverse_kind[image_kind])
    return "-".join(parts)
