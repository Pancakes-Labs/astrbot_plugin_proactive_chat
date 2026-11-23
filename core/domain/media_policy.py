"""富媒体组件分类、占位与来源安全策略。

本模块只做纯计算，不导入 AstrBot：组件分类全部基于类名，
保证 domain 层不反向依赖框架，也方便单测直接构造假组件。

设计取向：平台能力不做静态登记，统一按“乐观放行 + 发送失败再降级为文本占位”处理。
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

# 组件类别
KIND_TEXT = "text"
KIND_MEDIA = "media"
KIND_JSON = "json"
KIND_INTERACTIVE = "interactive"
KIND_UNKNOWN = "unknown"

# 可承载媒体的组件类名 -> 媒体类型
MEDIA_CLASS_MAP = {
    "Image": "image",
    "Record": "record",
    "Video": "video",
    "File": "file",
}

# 有交互语义、主动消息链路会剥离的组件类名
INTERACTIVE_CLASS_MAP = {
    "At": "at",
    "AtAll": "at_all",
    "Reply": "reply",
    "Poke": "poke",
    "Face": "face",
    "Dice": "dice",
    "RPS": "rps",
    "Shake": "shake",
}

# 纯文本组件类名：只有这些组件的 text 可被清洗与丢弃
TEXT_CLASS_NAMES = frozenset({"Plain", "Unknown"})
JSON_CLASS_NAMES = frozenset({"Json"})

# 媒体类型的文本占位（降级与存档共用同一套文案）
MEDIA_PLACEHOLDERS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片]",
}

# base64 载荷上限：超出视为不可投递，避免超大内联数据进入消息链
MAX_BASE64_CHARS = 16 * 1024 * 1024

_REMOTE_PREFIXES = ("http://", "https://", "data:")
_BASE64_PREFIX = "base64://"
_FILE_PREFIX = "file://"

# 各媒体类型的候选来源字段名。
# File 的 file 是 property，取值可能触发同步下载，因此只用 file_
_SOURCE_ATTRS = {
    "image": ("url", "file", "path"),
    "record": ("url", "file", "path"),
    "video": ("url", "file", "path"),
    "file": ("url", "file_"),
    "json": (),
}


def component_class_name(comp: object) -> str:
    """返回组件类名；无法获取时返回空串。"""
    return type(comp).__name__


def media_type_of(comp: object) -> str:
    """返回组件对应的媒体类型，非媒体组件返回空串。"""
    name = component_class_name(comp)
    if name in MEDIA_CLASS_MAP:
        return MEDIA_CLASS_MAP[name]
    if name in JSON_CLASS_NAMES:
        return "json"
    return ""


def component_kind(comp: object) -> str:
    """返回组件所属的粗分类。"""
    name = component_class_name(comp)
    if name in TEXT_CLASS_NAMES:
        return KIND_TEXT
    if name in MEDIA_CLASS_MAP:
        return KIND_MEDIA
    if name in JSON_CLASS_NAMES:
        return KIND_JSON
    if name in INTERACTIVE_CLASS_MAP:
        return KIND_INTERACTIVE
    return KIND_UNKNOWN


def is_text_component(comp: object) -> bool:
    return component_kind(comp) == KIND_TEXT


def is_media_component(comp: object) -> bool:
    """媒体与 JSON 卡片都算“非文本锚点”，分段时作为批次锚点。"""
    return component_kind(comp) in (KIND_MEDIA, KIND_JSON)


def text_of(comp: object) -> str:
    """读取纯文本组件的正文；其余组件返回空串。"""
    if not is_text_component(comp):
        return ""
    text = getattr(comp, "text", None)
    return text if isinstance(text, str) else ""


def sanitize_display_name(name: object) -> str:
    """归一化展示文件名，去掉路径与危险字符。"""
    if not isinstance(name, str) or not name.strip():
        return ""
    normalized = name.replace("\\", "/").replace("\x00", "")
    base = PurePosixPath(normalized).name.strip()
    if base in {"", ".", ".."}:
        return ""
    for char in ':*?"<>|':
        base = base.replace(char, "_")
    return base


def media_display_name(comp: object) -> str:
    """尽力读取媒体的展示名（文件名），读不到返回空串。"""
    media_type = media_type_of(comp)
    if media_type == "file":
        return sanitize_display_name(getattr(comp, "name", None))
    for attr in ("name", "path", "file", "url"):
        base = sanitize_display_name(getattr(comp, attr, None))
        if base:
            return base
    return ""


def media_placeholder(comp: object) -> str:
    """返回媒体组件的文本占位；未知媒体回退为 [媒体]。"""
    media_type = media_type_of(comp)
    if media_type == "file":
        name = media_display_name(comp)
        return f"[文件 {name}]" if name else MEDIA_PLACEHOLDERS["file"]
    return MEDIA_PLACEHOLDERS.get(media_type, "[媒体]")


def _file_uri_to_path(uri: str) -> Path:
    """把 file:// URI 转成本地路径（兼容 Windows 盘符）。"""
    parsed = urlparse(uri)
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        path = f"//{parsed.netloc}{path}"
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if not path:
        return Path(uri[len(_FILE_PREFIX) :])
    return Path(path)


def _is_under(path: Path, roots: list[Path]) -> bool:
    """判断路径是否位于允许的根目录之内。"""
    if not roots:
        return False
    try:
        resolved = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            root_resolved = root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
    return False


def _media_sources(comp: object, media_type: str) -> list[str]:
    sources: list[str] = []
    for attr in _SOURCE_ATTRS.get(media_type, ()):
        try:
            value = getattr(comp, attr, None)
        except Exception:  # noqa: BLE001 - File.file 等 property 可能抛错
            continue
        if isinstance(value, str) and value.strip():
            sources.append(value.strip())
    return sources


def is_safe_media_source(
    comp: object, allowed_dirs: list[str] | None = None
) -> tuple[bool, str]:
    """判断媒体来源是否允许发送。

    规则：
    - http/https/data/base64 一律放行（base64 有长度上限）；
    - 本地路径（含 file:// 与裸路径）只允许落在 allowed_dirs 之内，
      避免模型或第三方插件诱导 Bot 泄漏任意本地文件。

    Returns:
        (是否安全, 不安全原因)。安全时原因为空串。
    """
    media_type = media_type_of(comp)
    if not media_type:
        return False, "not_media"
    if media_type == "json":
        return True, ""

    sources = _media_sources(comp, media_type)
    if not sources:
        return False, "missing_source"

    roots = [Path(item) for item in (allowed_dirs or []) if item]
    local_seen = False
    for source in sources:
        lowered = source.lower()
        if lowered.startswith(_BASE64_PREFIX):
            if len(source) > MAX_BASE64_CHARS:
                return False, "base64_too_large"
            return True, ""
        if lowered.startswith(_REMOTE_PREFIXES):
            return True, ""
        if lowered.startswith(_FILE_PREFIX):
            path = _file_uri_to_path(source)
        else:
            path = Path(source)
        local_seen = True
        if _is_under(path, roots):
            return True, ""

    return False, ("path_not_allowed" if local_seen else "missing_source")


def describe_component(comp: object, reason: str = "") -> dict[str, str]:
    """生成可用于日志与报告的被剥离组件描述。"""
    media_type = media_type_of(comp)
    detail = {
        "component": component_class_name(comp),
        "type": media_type or component_class_name(comp).lower(),
        "reason": reason,
    }
    name = media_display_name(comp)
    if name:
        detail["name"] = name
    return detail


__all__ = [
    "KIND_INTERACTIVE",
    "KIND_JSON",
    "KIND_MEDIA",
    "KIND_TEXT",
    "KIND_UNKNOWN",
    "MAX_BASE64_CHARS",
    "MEDIA_PLACEHOLDERS",
    "TEXT_CLASS_NAMES",
    "component_class_name",
    "component_kind",
    "describe_component",
    "is_media_component",
    "is_safe_media_source",
    "is_text_component",
    "media_display_name",
    "media_placeholder",
    "media_type_of",
    "sanitize_display_name",
    "text_of",
]
