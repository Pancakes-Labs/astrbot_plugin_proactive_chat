"""主动消息在对话历史中的存档文案。

主动消息在协议层是伪事件：模型不会自己开口，只能由宿主注入一条 user 消息
触发它运行。这条注入文本只对当前这一次有意义，但它过去会被完整写进
对话历史，导致长会话里堆积大量重复的操作说明。

本模块把两件事拆开：

- 当前请求：仍然使用完整提示词，由 PROMPT 阶段负责，本模块不参与；
- 历史存档：只记录发生过一次主动消息事件这一事实。

存档一律使用陈述句。标题用【】而不是 []：提示词内部的 [系统任务]、[最终指令]、
[安全约束] 是指令块语法，历史标记若沿用同一种括号，模型容易把事件当成新的指令。
"""

from __future__ import annotations

from typing import Final

from .content_text import reduce_record_text

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

# 标记固定前缀，供扫描历史时识别既有记录。
# 其他插件（记忆、摘要）也可复用该前缀做过滤。
MARKER_PREFIX: Final = "【系统事件记录】主动消息触发"

# 段首：历史里没有任何既有标记时写入，一次性交代清楚：
# 1. 这条 user 记录不是真人输入；
# 2. 紧随其后的 assistant 消息是模型自己的主动发言。
_SEGMENT_HEAD_TEMPLATES: Final[dict[str, str]] = {
    "friend": (
        "【系统事件记录】主动消息触发（第 {index} 次，发出时对方未回应）。"
        "紧随其后的助手消息是你当时的主动发言，对方并未发送任何内容。"
    ),
    "group": (
        "【系统事件记录】主动消息触发（第 {index} 次，发出时群内无新消息）。"
        "紧随其后的助手消息是你当时在群里的主动发言，群内并没有人在对你说话。"
    ),
}

# 段内：同一段沉默链里已有段首解释，只需补增量事实。
_CONTINUATION_TEMPLATES: Final[dict[str, str]] = {
    "friend": "【系统事件记录】主动消息触发（第 {index} 次），发出时对方仍未回应。",
    "group": "【系统事件记录】主动消息触发（第 {index} 次），发出时群内仍无新消息。",
}

_SESSION_TYPE_GROUP: Final = "group"
_SESSION_TYPE_FRIEND: Final = "friend"

# 扫描历史时最多回看的条数。段首只可能在沉默链开头，
# 超出这个范围仍找不到标记就按「没有段首」处理，宁可多重锚定一次。
MARKER_SCAN_LIMIT: Final = 200


def normalize_archive_mode(value: object) -> str:
    """把配置值归一为合法存档形态，非法值回退默认。"""
    mode = str(value or "").strip().lower()
    return mode if mode in ARCHIVE_MODES else DEFAULT_ARCHIVE_MODE


def contains_proactive_marker(history: object, limit: int = MARKER_SCAN_LIMIT) -> bool:
    """判断历史里是否已存在主动消息标记。

    段首文案只在历史里确实没有解释时才写。仅凭未回复计数判断不够：
    清空历史、切换存档形态、历史被截断之后，计数仍大于 0 而标记已不存在，
    续接文案会让模型看到孤立的第 N 次却没有任何上下文。
    """
    if not isinstance(history, list) or not history:
        return False

    records = history[-limit:] if limit > 0 else history
    for record in reversed(records):
        content = record.get("content") if isinstance(record, dict) else record
        text = reduce_record_text(content).strip()
        if not text:
            continue
        if text.startswith(MARKER_PREFIX) or text == BARE_MARKER:
            return True
    return False


def build_proactive_history_text(
    mode: object,
    *,
    full_text: str = "",
    unanswered_index: int = 1,
    session_type: str = "",
    has_prior_marker: bool = True,
) -> str:
    """生成本轮主动消息写入对话历史的 user 侧文本。

    Args:
        mode: 存档形态，非法值按 semantic_event 处理。
        full_text: 本轮实际发送的完整提示词，仅在 full 形态下使用。
        unanswered_index: 本轮在用户沉默链里的序号，从 1 开始。
        session_type: friend / group，决定措辞里的指代对象。
        has_prior_marker: 历史里是否已有主动消息标记。为 False 时即使序号
            大于 1 也写段首文案，避免出现没有解释的孤立续接记录。

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

    try:
        index = max(1, int(unanswered_index))
    except (TypeError, ValueError):
        index = 1

    # 计数异常或历史里没有既有解释时都按段首写。
    templates = (
        _SEGMENT_HEAD_TEMPLATES
        if index <= 1 or not has_prior_marker
        else _CONTINUATION_TEMPLATES
    )
    key = (
        _SESSION_TYPE_GROUP
        if str(session_type or "").strip().lower() == _SESSION_TYPE_GROUP
        else _SESSION_TYPE_FRIEND
    )
    return templates[key].format(index=index)


__all__ = [
    "ARCHIVE_MODES",
    "ARCHIVE_MODE_BARE_MARKER",
    "ARCHIVE_MODE_EMPTY",
    "ARCHIVE_MODE_FULL",
    "ARCHIVE_MODE_SEMANTIC_EVENT",
    "BARE_MARKER",
    "DEFAULT_ARCHIVE_MODE",
    "MARKER_PREFIX",
    "MARKER_SCAN_LIMIT",
    "build_proactive_history_text",
    "contains_proactive_marker",
    "normalize_archive_mode",
]
