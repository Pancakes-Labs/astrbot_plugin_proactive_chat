"""提示词占位符渲染工具。

把散落在各服务里的模板替换集中到一处：
{{current_time}} / {{unanswered_count}} / {{platform_history_lines}} /
{{platform_history_last_time}}。

参数传 None 表示本次不替换该占位符（原样保留），
这样同一套实现可以同时服务 PROMPT 阶段与平台流水格式化。
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER_CURRENT_TIME = "{{current_time}}"
PLACEHOLDER_UNANSWERED_COUNT = "{{unanswered_count}}"
PLACEHOLDER_PLATFORM_HISTORY_LINES = "{{platform_history_lines}}"
PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME = "{{platform_history_last_time}}"

PLACEHOLDERS: tuple[str, ...] = (
    PLACEHOLDER_CURRENT_TIME,
    PLACEHOLDER_UNANSWERED_COUNT,
    PLACEHOLDER_PLATFORM_HISTORY_LINES,
    PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME,
)


def contains_placeholder(text: Any, placeholder: str) -> bool:
    """判断文本中是否已出现指定占位符（用于避免重复注入运行时块）。"""
    if not isinstance(text, str) or not text:
        return False
    return bool(placeholder) and placeholder in text


def render_placeholders(
    text: Any,
    *,
    current_time: str | None = None,
    unanswered_count: int | None = None,
    platform_history_lines: str | None = None,
    platform_history_last_time: str | None = None,
) -> str:
    """按给定值替换占位符。

    只扫描原模板一遍，替换进去的内容不会再次被解析。
    这样历史消息正文里出现的 {{current_time}} 字面量会原样保留，
    不会被后续步骤当成占位符二次改写。

    Args:
        text: 模板正文，非字符串时按字符串形式处理。
        current_time: 当前时间文本；None 表示不替换。
        unanswered_count: 未回复累计次数；None 表示不替换。
        platform_history_lines: 平台流水正文；None 表示不替换。
        platform_history_last_time: 流水最后一条的发送时间；None 表示不替换。

    Returns:
        替换后的文本。
    """
    if text is None:
        return ""
    template = text if isinstance(text, str) else str(text)

    values: dict[str, str] = {}
    if current_time is not None:
        values[PLACEHOLDER_CURRENT_TIME] = str(current_time)
    if unanswered_count is not None:
        values[PLACEHOLDER_UNANSWERED_COUNT] = str(unanswered_count)
    if platform_history_lines is not None:
        values[PLACEHOLDER_PLATFORM_HISTORY_LINES] = str(platform_history_lines)
    if platform_history_last_time is not None:
        values[PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME] = str(platform_history_last_time)
    if not values:
        return template

    # 一次扫描完成全部替换；re.sub 不会对替换结果再次匹配。
    pattern = "|".join(re.escape(key) for key in values)
    return re.sub(pattern, lambda m: values[m.group(0)], template)


def strip_placeholders(text: Any) -> str:
    """移除模板中所有仍未替换的占位符。

    仅用于提示词兜底：模板里写了本链路不提供的占位符时，
    宁可留空也不要让模型看到 {{platform_history_lines}} 这种字面标记。
    """
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    pattern = "|".join(re.escape(key) for key in PLACEHOLDERS)
    return re.sub(pattern, "", text)


__all__ = [
    "PLACEHOLDERS",
    "PLACEHOLDER_CURRENT_TIME",
    "PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME",
    "PLACEHOLDER_PLATFORM_HISTORY_LINES",
    "PLACEHOLDER_UNANSWERED_COUNT",
    "contains_placeholder",
    "render_placeholders",
    "strip_placeholders",
]
