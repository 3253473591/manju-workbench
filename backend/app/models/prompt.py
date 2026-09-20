"""提示词版本。

字段名沿用 docs/architecture/database-design.md 的 `node_id`；在本工程里它指向
`media_nodes.id`——因为按 PRD，只有图片/视频方案节点才拥有 B 文本与版本。

隐藏 A、完整内部组装/优化文本只存服务端，绝不进入任何 API 响应、日志或来源查询
（PRD §5.4、api/conventions.md §5）。
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Field

from app.core.utils import new_ulid
from app.models.common import TimestampMixin, enum_field
from app.models.enums import PromptSource


class PromptVersion(TimestampMixin, table=True):
    __tablename__ = "prompt_versions"

    id: str = Field(default_factory=new_ulid, primary_key=True, max_length=26)
    node_id: str = Field(foreign_key="media_nodes.id", index=True, max_length=26)
    revision_no: int = Field(default=1, nullable=False)
    #: 用户可见、可编辑的描述（B）
    visible_text: str = Field(default="")
    #: 隐藏预设版本号，只有版本标识，没有正文
    hidden_template_version: str = Field(default="", max_length=64)
    #: 隐藏预设正文（A）——服务端专用，任何响应都不得返回
    hidden_preset_text: str = Field(default="")
    #: 服务端优化 A+B 后的最终生图输入——服务端专用
    final_input_text: str = Field(default="")
    final_input_hash: str = Field(default="", max_length=128)
    source: PromptSource = enum_field(PromptSource, PromptSource.ai, length=16)
    created_by: Optional[str] = Field(default=None, max_length=26)
