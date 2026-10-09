"""文本归约纯函数。

集中收敛此前散落在 chat_flow / llm_adapter 中的文本提取逻辑，
每一支都严格对应旧实现，避免出现细微差异。
"""

from __future__ import annotations

from typing import Any

# 内容分段中可能承载文本的键名（不同 AstrBot 版本/场景存在差异）。
_TEXT_KEYS: tuple[str, ...] = ("text", "content", "message", "value")


def reduce_record_text(content: Any) -> str:
    """从对话历史记录的 content 字段提取纯文本。"""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                for key in _TEXT_KEYS:
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        chunks.append(value)
                        break
        return "".join(chunks)

    if isinstance(content, dict):
        for key in _TEXT_KEYS:
            value = content.get(key)
            if isinstance(value, str):
                return value

    return ""


def reduce_segment_text(content: Any) -> str:
    """将历史消息 content 归约为纯文本字符串。

    额外兼容带 .text / .get_text() 的段对象。
    """
    if isinstance(content, str):
        return content

    # 列表形态：逐段抽取文本并拼接，无法识别的段直接忽略。
    if isinstance(content, list):
        text_content = ""
        for segment in content:
            if isinstance(segment, dict):
                for key in _TEXT_KEYS:
                    value = segment.get(key)
                    if isinstance(value, str) and value:
                        text_content += value
                        break
            elif hasattr(segment, "text"):
                text_content += getattr(segment, "text", "")
            elif hasattr(segment, "get_text"):
                text_content += segment.get_text()
            elif isinstance(segment, str):
                text_content += segment
        return text_content

    # 字典形态：按候选键取首个非空字符串。
    if isinstance(content, dict):
        for key in _TEXT_KEYS:
            value = content.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    # 兜底：None 视为空串，其余类型直接转字符串。
    return "" if content is None else str(content)
