"""主动消息执行上下文（贯穿整条流水线）。

这是流水线的唯一可变载体：Stage 从它读取输入、写入产物。
所有阶段产物均以强类型模型承载，禁止再用 dict 传递（旧 ctx.extra 已废弃）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .context_block import BlockKind, ContextBlock, ContextBundle
from .delivery_report import DeliveryReport
from .domain_enums import TriggerSource
from .llm_outcome import LlmOutcome
from .persona_resolution import PersonaResolution
from .prompt_block import PromptBundle
from .session_config import SessionConfig
from .session_key import SessionKey


@dataclass
class ProactiveContext:
    """一次主动消息执行的上下文。"""

    # --- 身份 ---
    session_id: str = ""
    session: SessionKey = None  # type: ignore[assignment]
    trigger: TriggerSource = TriggerSource.SCHEDULE

    # --- 配置与运行态 ---
    config: SessionConfig | None = None
    conversation_id: str = ""
    # 平台对话对象，供 LLM 请求装配使用（不属于可持久化上下文块）。
    conversation: Any = None
    unanswered_count: int = 0
    started_at: float = field(default_factory=time.time)
    # 本次生效人格的解析结论（供后续阶段复用，避免重复解析）。
    persona: PersonaResolution | None = None

    # --- 各阶段产物 ---
    contexts: ContextBundle = field(default_factory=ContextBundle)
    prompts: PromptBundle = field(default_factory=PromptBundle)
    outcome: LlmOutcome | None = None
    delivery: DeliveryReport | None = None

    # --- 事件与追踪 ---
    event: Any = None
    # 任务开始时的状态快照（用于生成期间新消息检测）
    start_state: dict[str, Any] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)
    abort_reason: str = ""

    # ------------------------------------------------------------------
    def mark(self, name: str) -> None:
        """记录已执行的 Stage 名称。"""
        self.trace.append(name)

    def abort(self, reason: str) -> None:
        """标记本次执行放弃并记录原因。"""
        self.abort_reason = reason

    @property
    def aborted(self) -> bool:
        return bool(self.abort_reason)

    # ------------------------------------------------------------------
    # 上下文块读写（替代旧 ctx.extra 的 history / system_prompt / platform_context）
    # ------------------------------------------------------------------
    def add_context(self, block: ContextBlock) -> None:
        """追加一个上下文块。"""
        self.contexts.add(block)

    def context_block(self, kind: BlockKind) -> ContextBlock | None:
        """返回指定类型的首个上下文块，不存在时返回 None。"""
        blocks = self.contexts.by_kind(kind)
        return blocks[0] if blocks else None

    @property
    def history(self) -> list:
        """稳定对话历史消息列表。"""
        block = self.context_block(BlockKind.STABLE_HISTORY)
        if block is not None and isinstance(block.content, list):
            return block.content
        return []

    @property
    def system_prompt(self) -> str:
        """系统提示词（人格设定）。"""
        block = self.context_block(BlockKind.SYSTEM_PROMPT)
        if block is not None and isinstance(block.content, str):
            return block.content
        return ""

    @property
    def platform_context(self) -> str:
        """平台流水等临时参考文本。"""
        block = self.context_block(BlockKind.TEMP_REFERENCE)
        if block is not None and isinstance(block.content, str):
            return block.content
        return ""

    # ------------------------------------------------------------------
    # 生成结果便捷访问
    # ------------------------------------------------------------------
    @property
    def response_text(self) -> str:
        """LLM 生成的纯文本。"""
        return self.outcome.text if self.outcome else ""

    @property
    def response_chain(self) -> list:
        """LLM 生成的消息链组件。"""
        return self.outcome.chain if self.outcome else []
