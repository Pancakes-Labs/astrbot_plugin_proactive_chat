"""主动消息在对话历史中的存档文案。

主动消息在协议层是伪事件：模型不会自己开口，只能由宿主注入一条 user 消息
触发它运行。这条注入文本只对当前这一次有意义，但它过去会被完整写进
对话历史，导致长会话里堆积大量重复的操作说明。

本模块把两件事拆开：

- 当前请求：仍然使用完整提示词，由 PROMPT 阶段负责，本模块不参与；
- 历史存档：只记录发生过一次主动消息事件这一事实。

语用上区分两种文本：

- 指令：请生成…… / 你必须……
- 事件：过去发生过…… / 对方没有发言

存档一律使用陈述句。标题用【】而不是 []：提示词内部的 [系统任务]、[最终指令]、
[安全约束] 是指令块语法，历史标记若沿用同一种括号，模型容易把事件当成新的指令。
"""

from __future__ import annotations

from typing import Final

# 存档形态。
ARCHIVE_MODE_SEMANTIC_EVENT: Final = "semantic_event"
ARCHIVE_MODE_FULL: Final = "full"
ARCHIVE_MODE_BARE_MARKER: Final = "bare_marker"
ARCHIVE_MODE_EMPTY: Final = "empty"

DEFAULT_ARCHIVE_MODE: Final = ARCHIVE_MODE_SEMANTIC_EVENT

ARCHIVE_MODES: Final = (
    ARCHIVE_MODE_SEMANTIC_EVENT,
    ARCHIVE_MODE_FULL,
    ARCHIVE_MODE_BARE_MARKER,
    ARCHIVE_MODE_EMPTY,
)

# 最短标记。empty 形态在部分 Provider 上会拒收空 user 消息，
# 导致整条历史静默丢失，调用方可用它作为兜底。
BARE_MARKER: Final = "[主动消息]"

# 段首：用户沉默链里的第一次主动消息。三个语义必须一次说清，后续不再重复：
# 1. 这是宿主程序产生的事件，不是用户发言；
# 2. 紧随其后的 assistant 消息是模型自己的主动发言；
# 3. 当时并没有人在对模型说话。
#
# 同一段里沉默与谁在说话的措辞必须整体一致：
# 私聊是具体的对方，群聊是没人接话。
_SEGMENT_HEAD_TEMPLATES: Final[dict[str, str]] = {
    "friend": (
        "【系统事件记录】主动消息触发（第 {index} 次，对方未回应）。"
        "紧随其后的助手消息是你当时的主动发言，对方并未发送任何内容。"
    ),
    "group": (
        "【系统事件记录】主动消息触发（第 {index} 次，群内无新消息）。"
        "紧随其后的助手消息是你当时在群里的主动发言，群内并没有人在对你说话。"
    ),
}

# 段内：用户仍未回复时的第 N 次。只补增量事实，避免重复解释同一件事。
_CONTINUATION_TEMPLATES: Final[dict[str, str]] = {
    "friend": "【系统事件记录】主动消息触发（第 {index} 次），对方仍未回应。",
    "group": "【系统事件记录】主动消息触发（第 {index} 次），群内仍无新消息。",
}

_SESSION_TYPE_GROUP: Final = "group"


def normalize_archive_mode(value: object) -> str:
    """把配置值归一为合法存档形态，非法值回退默认。"""
    mode = str(value or "").strip().lower()
    return mode if mode in ARCHIVE_MODES else DEFAULT_ARCHIVE_MODE


def build_proactive_history_text(
    mode: object,
    *,
    full_text: str = "",
    unanswered_index: int = 1,
    session_type: str = "",
) -> str:
    """生成本轮主动消息写入对话历史的 user 侧文本。

    Args:
        mode: 存档形态，非法值按 semantic_event 处理。
        full_text: 本轮实际发送的完整提示词，仅在 full 形态下使用。
        unanswered_index: 本轮在用户沉默链里的序号，从 1 开始。
            调用方需传发送后的计数，即上下文里的 unanswered_count + 1。
        session_type: friend / group，决定沉默与发言对象的措辞。

    Returns:
        写入历史的 user 文本；empty 形态返回空串，由调用方决定兜底。
    """
    normalized = normalize_archive_mode(mode)

    if normalized == ARCHIVE_MODE_EMPTY:
        return ""
    if normalized == ARCHIVE_MODE_BARE_MARKER:
        return BARE_MARKER
    if normalized == ARCHIVE_MODE_FULL:
        return str(full_text or "")

    # 计数损坏、被清零或异常时一律按段首处理：
    # 宁可多重锚定一次语义，也不要让历史上的事件失去解释。
    try:
        index = max(1, int(unanswered_index))
    except (TypeError, ValueError):
        index = 1

    key = (
        _SESSION_TYPE_GROUP
        if str(session_type or "").strip().lower() == _SESSION_TYPE_GROUP
        else "friend"
    )
    templates = _SEGMENT_HEAD_TEMPLATES if index == 1 else _CONTINUATION_TEMPLATES
    return templates[key].format(index=index)


__all__ = [
    "ARCHIVE_MODES",
    "ARCHIVE_MODE_BARE_MARKER",
    "ARCHIVE_MODE_EMPTY",
    "ARCHIVE_MODE_FULL",
    "ARCHIVE_MODE_SEMANTIC_EVENT",
    "BARE_MARKER",
    "DEFAULT_ARCHIVE_MODE",
    "build_proactive_history_text",
    "normalize_archive_mode",
]
