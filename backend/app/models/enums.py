"""领域枚举。

约定：**每个成员的名称与其字符串值完全相同**（全小写）。这样无论 ORM 按 name 还是
按 value 落库，存的都是同一个字符串，避免 SQLAlchemy Enum 在 name/value 之间的歧义。
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    user = "user"
    manager = "manager"
    admin = "admin"


class AccountStatus(str, Enum):
    active = "active"
    disabled = "disabled"


class ProjectStatus(str, Enum):
    active = "active"
    archived = "archived"


class EpisodeStatus(str, Enum):
    draft = "draft"
    active = "active"
    archived = "archived"


class DocumentKind(str, Enum):
    outline = "outline"
    setting = "setting"
    script = "script"
    clip = "clip"


class DocumentStatus(str, Enum):
    draft = "draft"
    saved = "saved"
    archived = "archived"


class RevisionSource(str, Enum):
    user = "user"
    ai = "ai"
    #: `import` 是 Python 保留字，成员名加下划线；落库仍按 value 存 "import"
    import_ = "import"


class CanvasNodeType(str, Enum):
    text = "text"
    shot = "shot"
    image = "image"
    video = "video"


class EdgeType(str, Enum):
    derives = "derives"
    references = "references"
    source = "source"


class AssetType(str, Enum):
    character = "character"
    scene = "scene"
    prop = "prop"


class AssetStatus(str, Enum):
    planned = "planned"
    active = "active"
    archived = "archived"


class MediaType(str, Enum):
    image = "image"
    video = "video"
    audio = "audio"
    file = "file"


class MediaKind(str, Enum):
    image_plan = "image_plan"
    image_result = "image_result"
    video_plan = "video_plan"
    video_result = "video_result"


class DecodeStatus(str, Enum):
    pending = "pending"
    verified = "verified"
    failed = "failed"


class DeliveryStatus(str, Enum):
    pending = "pending"
    verified = "verified"
    failed = "failed"


class LogicalStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class PhysicalStatus(str, Enum):
    reserved = "reserved"
    accepted = "accepted"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    unknown = "unknown"


class BillingStatus(str, Enum):
    unknown = "unknown"
    estimated = "estimated"
    confirmed = "confirmed"
    mock = "mock"


class ProviderCapability(str, Enum):
    text = "text"
    image = "image"
    video = "video"


class PromptSource(str, Enum):
    ai = "ai"
    user = "user"
    #: 同 RevisionSource：成员名避开保留字，落库按 value 存 "import"
    import_ = "import"
    template = "template"


class Operation(str, Enum):
    """一次用户操作的语义。每个 operation 对应一条 generation_submission。"""

    # 文本与提示词
    outline_optimize = "outline_optimize"
    setting_generate = "setting_generate"
    script_generate = "script_generate"
    script_optimize = "script_optimize"
    script_split_clips = "script_split_clips"
    clip_optimize = "clip_optimize"
    # 资产规划
    plan_characters = "plan_characters"
    plan_scenes = "plan_scenes"
    plan_props = "plan_props"
    # 图片
    image_plan_create = "image_plan_create"
    image_plan_optimize = "image_plan_optimize"
    image_draw = "image_draw"
    image_derive = "image_derive"
    # 视频
    video_plan_create = "video_plan_create"
    video_plan_optimize = "video_plan_optimize"
    video_draw = "video_draw"


class AttemptTarget(str, Enum):
    """attempt_links 的目标类型，用于追溯与去重计费。"""

    document = "document"
    clip = "clip"
    canvas_node = "canvas_node"
    media_node = "media_node"
    asset = "asset"


#: 隐藏预设：只在服务端组装，绝不通过 API 返回原文（PRD §5.4）。
HIDDEN_PRESET_VERSION = "preset-v1"
