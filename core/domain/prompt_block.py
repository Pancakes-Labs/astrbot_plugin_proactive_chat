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
        text: 提示词正文（占位符已在 PROMPT 阶段替换完毕）。
        role: 语义角色，如 task / persona / style。
        source: 产出来源（feature 名称），便于调试与去重。
        metadata: 扩展信息（模板 id / 名称 / 原始正文等）。
    """

    text: str = ""
    role: str = "task"
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptExtra:
    """随请求一并携带的运行时附加内容块（平台流水、时间等）。

    这类内容只在本轮请求中临时注入，不参与对话历史存档，
    因此与 PromptBlock 分开存放。
    """

    part: Any = None
    source: str = ""


@dataclass
class PromptBundle:
    """PROMPT 阶段汇总结果。"""

    blocks: list[PromptBlock] = field(default_factory=list)
    extras: list[PromptExtra] = field(default_factory=list)

    def add(self, block: PromptBlock) -> None:
        self.blocks.append(block)

    def add_extra(self, part: Any, source: str = "") -> None:
        """登记一个运行时附加内容块。"""
        if part is None:
            return
        self.extras.append(PromptExtra(part=part, source=source))

    def remove_by_source(self, source: str) -> int:
        """移除指定来源写入的提示词块，返回移除数量。

        同一阶段重复装配时只清理自身产物，避免误删其他阶段的块。
        """
        before = len(self.blocks)
        self.blocks = [b for b in self.blocks if b.source != source]
        return before - len(self.blocks)

    def remove_extras_by_source(self, source: str) -> int:
        """移除指定来源写入的附加内容块，返回移除数量。"""
        before = len(self.extras)
        self.extras = [e for e in self.extras if e.source != source]
        return before - len(self.extras)

    def merge_text(self, separator: str = "\n\n") -> str:
        """合并全部提示词正文。"""
        return separator.join(b.text for b in self.blocks if b.text)

    @property
    def extra_parts(self) -> list[Any]:
        """按登记顺序返回附加内容块本体。"""
        return [e.part for e in self.extras if e.part is not None]
