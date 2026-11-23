"""文本归约纯函数。

集中收敛此前散落在 chat_flow / llm_adapter 中的文本提取逻辑，
每一支都严格对应旧实现，避免出现细微差异。
"""

from __future__ import annotations

from typing import Any

from .media_policy import MEDIA_PLACEHOLDERS

# 内容分段中可能承载文本的键名（不同 AstrBot 版本/场景存在差异）。
_TEXT_KEYS: tuple[str, ...] = ("text", "content", "message", "value")

# 结构化媒体分段 -> 文本占位。模型读历史时需要知道“发过图/语音”，
# 否则纯媒体轮次在历史里就是一片空白。
_SEGMENT_TYPE_PLACEHOLDERS: dict[str, str] = {
    "image": MEDIA_PLACEHOLDERS["image"],
    "image_url": MEDIA_PLACEHOLDERS["image"],
    "record": MEDIA_PLACEHOLDERS["record"],
    "audio": MEDIA_PLACEHOLDERS["record"],
    "audio_url": MEDIA_PLACEHOLDERS["record"],
    "video": MEDIA_PLACEHOLDERS["video"],
}


def _placeholder_for_segment(segment: dict) -> str:
    """按分段类型返回占位文本；非媒体分段返回空串。"""
    seg_type = str(segment.get("type") or "").lower()
    if seg_type == "file":
        name = segment.get("name") or segment.get("filename")
        return f"[文件 {name}]" if name else MEDIA_PLACEHOLDERS["file"]
    return _SEGMENT_TYPE_PLACEHOLDERS.get(seg_type, "")


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

    # 列表形态：逐段抽取文本并拼接，媒体段按类型补占位。
    if isinstance(content, list):
        text_content = ""
        for segment in content:
            if isinstance(segment, dict):
                matched = False
                for key in _TEXT_KEYS:
                    value = segment.get(key)
                    if isinstance(value, str) and value:
                        text_content += value
                        matched = True
                        break
                if not matched:
                    text_content += _placeholder_for_segment(segment)
            elif hasattr(segment, "text"):
                text_content += getattr(segment, "text", "")
            elif hasattr(segment, "get_text"):
                text_content += segment.get_text()
            elif isinstance(segment, str):
                text_content += segment
        return text_content

    # 字典形态：按候选键取首个非空字符串，媒体段补占位。
    if isinstance(content, dict):
        for key in _TEXT_KEYS:
            value = content.get(key)
            if isinstance(value, str) and value:
                return value
        return _placeholder_for_segment(content)

    # 兜底：None 视为空串，其余类型直接转字符串。
    return "" if content is None else str(content)
