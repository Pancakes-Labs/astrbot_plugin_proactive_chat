"""提示词块数据模型。

流水线的 PROMPT 阶段产出一个或多个 PromptBlock，
支持“多提示词模板注入”，替换现有单一 proactive_prompt。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptBlock:
    """单个提示词块。

    Attributes:
        text: 提示词正文（支持 {{current_time}} / {{unanswered_count}} 占位符）。
        role: 语义角色，如 task / persona / style。
        source: 产出来源（feature 名称），便于调试与去重。
        metadata: 扩展信息。
    """

    text: str = ""
    role: str = "task"
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptBundle:
    """PROMPT 阶段汇总结果。"""

    blocks: list[PromptBlock] = field(default_factory=list)

    def add(self, block: PromptBlock) -> None:
        self.blocks.append(block)

    def merge_text(self, separator: str = "\n\n") -> str:
        return separator.join(b.text for b in self.blocks if b.text)
