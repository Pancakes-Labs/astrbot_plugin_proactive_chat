"""LLM 生成结果数据模型。

统一承载文本、消息链、工具调用轨迹等，供 DELIVER/RECORD 阶段使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRecord:
    """单次工具调用记录（为链式工具调用预留）。"""

    name: str = ""
    arguments: Any = None
    result: Any = None


@dataclass
class LlmOutcome:
    """一次 LLM 调用的产出。"""

    text: str = ""
    # 原始响应对象（LLMResponse），供后置钩子改写与存档校验使用。
    response: Any = None
    # 消息链组件（文本 + 媒体的完整载体）。
    chain: list = field(default_factory=list)
    # 本轮实际使用的用户提示词（钩子处理后的最终形态）。
    final_user_prompt: str = ""
    # 工具调用轨迹（默认空，链式工具调用功能接入后填充）。
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    # Provider 标识，便于遥测与调试。
    provider_id: str = ""

    @property
    def has_content(self) -> bool:
        return bool(self.text) or bool(self.chain)
