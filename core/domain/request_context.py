"""主动消息执行上下文（贯穿整条流水线）。

这是流水线的唯一可变载体：Stage 从它读取输入、写入产物。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .context_block import ContextBundle
from .delivery_report import DeliveryReport
from .domain_enums import TriggerSource
from .llm_outcome import LlmOutcome
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
    unanswered_count: int = 0
    started_at: float = field(default_factory=time.time)

    # --- 各阶段产物 ---
    contexts: ContextBundle = field(default_factory=ContextBundle)
    prompts: PromptBundle = field(default_factory=PromptBundle)
    outcome: LlmOutcome | None = None
    delivery: DeliveryReport | None = None

    # --- 事件与追踪 ---
    event: Any = None
    # 各阶段之间传递的临时数据（history/system_prompt/response_text 等）
    extra: dict[str, Any] = field(default_factory=dict)
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
