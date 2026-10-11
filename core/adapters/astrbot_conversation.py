"""AstrBot 对话历史适配器：读取、存档与回读校验。

由旧 core/chat_flow.py 与 core/llm_adapter.py 中与
conversation_manager 交互的逻辑等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from astrbot.api import logger

from ..domain.content_text import reduce_record_text

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.agent.message import (
        AssistantMessageSegment,
        TextPart,
        UserMessageSegment,
    )
except ImportError:  # pragma: no cover
    AssistantMessageSegment = None  # type: ignore[assignment]
    TextPart = None  # type: ignore[assignment]
    UserMessageSegment = None  # type: ignore[assignment]


def extract_record_text(content: Any) -> str:
    """从对话历史记录的 content 字段中提取纯文本。

    直接复用 domain 层的文本归约实现，保持全项目语义一致。
    """
    return reduce_record_text(content)


async def verify_message_persisted(
    conversation_manager: Any,
    session_id: str,
    conv_id: str,
    assistant_response: str,
) -> bool:
    """回读对话记录，校验本次主动消息是否真正落盘。

    从历史末尾向前查找最近一条 assistant 记录并做包含匹配；
    任何异常或未命中都返回 False。
    """
    target = (assistant_response or "").strip()
    if not target:
        return False
    try:
        conversation = await conversation_manager.get_conversation(session_id, conv_id)
        if not conversation or not conversation.history:
            return False
        history = conversation.history
        if isinstance(history, str):
            history = json.loads(history)
        if not isinstance(history, list):
            return False
        for record in reversed(history):
            if not isinstance(record, dict) or record.get("role") != "assistant":
                continue
            extracted = reduce_record_text(record.get("content", ""))
            if not extracted.strip():
                continue
            return target in extracted
        return False
    except Exception as e:
        logger.debug(f"[主动消息] 回读校验对话历史失败喵: {e}")
        return False


async def add_message_pair(
    conversation_manager: Any,
    conv_id: str,
    user_prompt: str,
    assistant_response: str,
) -> None:
    """存档一对 user/assistant 消息到对话历史。

    依赖 AstrBot 的消息段 API；旧版本缺失时抛出运行时错误。
    """
    if UserMessageSegment is None or TextPart is None:
        raise RuntimeError("当前 AstrBot 版本缺少消息段 API")
    user_msg_obj = UserMessageSegment(content=[TextPart(text=user_prompt)])
    assistant_msg_obj = AssistantMessageSegment(
        content=[TextPart(text=assistant_response)]
    )
    await conversation_manager.add_message_pair(
        cid=conv_id,
        user_message=user_msg_obj,
        assistant_message=assistant_msg_obj,
    )


async def load_conversation_history(conversation: Any) -> list:
    """从 conversation 对象解析对话历史列表。

    兼容 history 为字符串（JSON）或已反序列化列表两种形态；
    解析失败或格式异常时一律回退为空列表。
    """
    pure_history_messages: list = []
    if conversation and conversation.history:
        try:
            if isinstance(conversation.history, str):
                pure_history_messages = await asyncio.to_thread(
                    json.loads, conversation.history
                )
            else:
                pure_history_messages = conversation.history
        except (json.JSONDecodeError, TypeError):
            logger.warning("[主动消息] 解析历史记录失败，使用空历史喵。")

    if not isinstance(pure_history_messages, list):
        logger.warning("[主动消息] 历史记录格式异常（非列表），已回退为空历史喵。")
        pure_history_messages = []
    return pure_history_messages
