"""上下文块数据模型。

流水线的 CONTEXT 阶段产出若干 ContextBlock，
由 DELIBERATE 阶段统一装配进 ProviderRequest，实现“多上下文来源”可插拔。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BlockKind(Enum):
    """上下文块类型（决定注入位置与是否落库）。"""

    STABLE_HISTORY = "stable_history"  # 稳定对话历史（可缓存前缀，落库）
    SYSTEM_PROMPT = "system_prompt"  # 系统提示词/人格设定（不落库）
    TEMP_REFERENCE = "temp_reference"  # 临时参考（平台流水等，不落库）
    RUNTIME_FACT = "runtime_fact"  # 运行时事实（时间/计数等，不落库）


@dataclass
class ContextBlock:
    """单个上下文块。"""

    kind: BlockKind
    content: Any = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextBundle:
    """CONTEXT 阶段汇总结果。"""

    blocks: list[ContextBlock] = field(default_factory=list)

    def by_kind(self, kind: BlockKind) -> list[ContextBlock]:
        return [b for b in self.blocks if b.kind is kind]

    def add(self, block: ContextBlock) -> None:
        self.blocks.append(block)

    def remove_by_source(self, source: str) -> int:
        """移除指定来源写入的块，返回移除数量。

        用于同一阶段重复装配时替换自身产物，避免误删其他阶段写入的块。
        """
        before = len(self.blocks)
        self.blocks = [b for b in self.blocks if b.source != source]
        return before - len(self.blocks)

    def clear(self) -> None:
        """清空全部上下文块（重新装配时使用）。"""
        self.blocks.clear()
