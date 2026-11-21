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


async def _load_records(
    conversation_manager: Any,
    session_id: str,
    conv_id: str,
) -> list:
    """读取对话历史记录列表；任何异常都返回空列表。

    只用于存档后的回读校验，失败不应影响主流程。
    """
    try:
        conversation = await conversation_manager.get_conversation(session_id, conv_id)
        if not conversation or not conversation.history:
            return []
        history = conversation.history
        if isinstance(history, str):
            history = json.loads(history)
        return history if isinstance(history, list) else []
    except Exception as e:
        logger.debug(f"[主动消息] 回读对话历史失败喵: {e}")
        return []


async def count_history_records(
    conversation_manager: Any,
    session_id: str,
    conv_id: str,
) -> int:
    """读取当前对话历史条数，用作本轮新增记录的边界基线。

    读不到时返回 0：调用方据此退化为扫描全量"""
    return len(await _load_records(conversation_manager, session_id, conv_id))


async def verify_pair_persisted(
    conversation_manager: Any,
    session_id: str,
    conv_id: str,
    user_text: str,
    assistant_text: str,
    since: int = 0,
    limit: int = 4,
) -> tuple[bool, bool]:
    """回读本轮的新增记录边界，判断 user / assistant 两侧是否都已落盘。

    Args:
        since: 写入前读取到的历史条数。0 表示基线不可用，退化为看末尾。
        limit: 基线不可用时回看的条数上限。

    Returns:
        (user_ok, assistant_ok)。user_text 为空时 user_ok 恒为 False，
        由调用方据此判断空内容是否被静默丢弃。
    """
    records = await _load_records(conversation_manager, session_id, conv_id)
    if not records:
        return False, False

    target_assistant = (assistant_text or "").strip()
    target_user = (user_text or "").strip()

    # 基线可用时只看新增部分；否则退化为末尾 limit 条。
    if since > 0:
        appended = records[since:]
    else:
        appended = records[-limit:]
    if not appended:
        return False, False

    # 基线正确时这一批就是本轮写入，两个标志来自同一对消息；
    # 基线失效（历史被并发改写）时仍有极小概率跨对匹配，故要求数量对齐。
    user_ok = False
    assistant_ok = False
    for record in appended:
        if not isinstance(record, dict):
            continue
        role = record.get("role")
        text = reduce_record_text(record.get("content", "")).strip()
        if not text:
            continue
        if role == "assistant" and target_assistant and target_assistant in text:
            assistant_ok = True
        elif role == "user" and target_user and target_user in text:
            user_ok = True

    return user_ok, assistant_ok


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
