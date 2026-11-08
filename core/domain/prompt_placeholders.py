"""提示词占位符渲染工具。

把散落在各服务里的模板替换集中到一处：
{{current_time}} / {{unanswered_count}} / {{platform_history_lines}} /
{{platform_history_last_time}}。

参数传 None 表示本次不替换该占位符（原样保留），
这样同一套实现可以同时服务 PROMPT 阶段与平台流水格式化。
"""

from __future__ import annotations

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

    替换顺序与旧实现保持一致：先注入流水正文，再替换计数与时间，
    保证正文里出现的同名字样也能被后续步骤处理。

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
    rendered = text if isinstance(text, str) else str(text)

    if platform_history_lines is not None:
        rendered = rendered.replace(
            PLACEHOLDER_PLATFORM_HISTORY_LINES, str(platform_history_lines)
        )
    if platform_history_last_time is not None:
        rendered = rendered.replace(
            PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME,
            str(platform_history_last_time),
        )
    if unanswered_count is not None:
        rendered = rendered.replace(PLACEHOLDER_UNANSWERED_COUNT, str(unanswered_count))
    if current_time is not None:
        rendered = rendered.replace(PLACEHOLDER_CURRENT_TIME, str(current_time))
    return rendered


__all__ = [
    "PLACEHOLDERS",
    "PLACEHOLDER_CURRENT_TIME",
    "PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME",
    "PLACEHOLDER_PLATFORM_HISTORY_LINES",
    "PLACEHOLDER_UNANSWERED_COUNT",
    "contains_placeholder",
    "render_placeholders",
]
