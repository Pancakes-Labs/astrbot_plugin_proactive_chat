"""提示词占位符渲染与能力感知消解。

本模块把占位符升级为带规格的注册表：
- requires 声明该占位符依赖哪项运行时能力；
- fallback 声明能力不满足时的替代文本；
- line_removable 允许整行删除，避免留下孤零零的标签行。

对外仍保留 render_placeholders / strip_placeholders 以兼容旧调用点，
新增 resolve_placeholders 作为推荐入口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PLACEHOLDER_CURRENT_TIME = "{{current_time}}"
PLACEHOLDER_UNANSWERED_COUNT = "{{unanswered_count}}"
PLACEHOLDER_PLATFORM_HISTORY_LINES = "{{platform_history_lines}}"
PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME = "{{platform_history_last_time}}"
PLACEHOLDER_CONVERSATION_TYPE = "{{conversation_type}}"

# 任意 {{...}} 形态，用于识别未知占位符（用户拼错时的提示依据）。
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")
# 行首的列表标记，整行删除时需要一并吃掉。
_LIST_PREFIX_RE = re.compile(r"^\s*[-*•]\s*")
# 只剩下标点与空白时视为“该行已无有效信息”。
_PUNCT_ONLY_RE = re.compile(r"^[\s，。、；：！？,.;:!?~～…()（）【】\[\]\"'“”‘’—\-]*$")
# “标签：值”结构在值缺失后会退化成“标签：。”，这种残句也应整行删除。
# 判定依据：占位符被移除后，行尾只剩冒号/等号 + 标点。
_DANGLING_LABEL_RE = re.compile(r"[：:＝=]\s*[。，、；！？,.;!?\s]*$")

# 能力键
CAP_TIME = "time"
CAP_UNANSWERED = "unanswered"
CAP_PLATFORM_HISTORY = "platform_history"
CAP_CONVERSATION_HISTORY = "conversation_history"
CAP_SESSION_TYPE = "session_type"


@dataclass(frozen=True)
class PlaceholderSpec:
    """单个占位符的规格声明。"""

    key: str
    label: str
    # 依赖的运行时能力；空表示恒定可用。
    requires: frozenset[str] = field(default_factory=frozenset)
    # 能力不满足时的替代文本；空表示允许整行删除或替换为空。
    fallback: str = ""
    # 是否允许在能力不满足时删除整行（仅对列表式单行生效）。
    line_removable: bool = False


PLACEHOLDER_SPECS: tuple[PlaceholderSpec, ...] = (
    PlaceholderSpec(
        key=PLACEHOLDER_CURRENT_TIME,
        label="当前时间",
        requires=frozenset({CAP_TIME}),
        fallback="未知",
    ),
    PlaceholderSpec(
        key=PLACEHOLDER_UNANSWERED_COUNT,
        label="未回复次数",
        requires=frozenset({CAP_UNANSWERED}),
        fallback="0",
    ),
    PlaceholderSpec(
        key=PLACEHOLDER_CONVERSATION_TYPE,
        label="会话类型",
        requires=frozenset({CAP_SESSION_TYPE}),
        fallback="会话",
    ),
    PlaceholderSpec(
        key=PLACEHOLDER_PLATFORM_HISTORY_LINES,
        label="平台聊天流水",
        requires=frozenset({CAP_PLATFORM_HISTORY}),
        # 流水是模板里最核心的载荷，不能整行删掉，只能给出明确占位说明。
        fallback="（本次没有可用的聊天记录）",
    ),
    PlaceholderSpec(
        key=PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME,
        label="流水最后一条时间",
        requires=frozenset({CAP_PLATFORM_HISTORY}),
        # 时间标签单独成行时整行删除更干净，避免“最后一条发生在：。”这种残句。
        line_removable=True,
    ),
)

SPEC_BY_KEY: dict[str, PlaceholderSpec] = {spec.key: spec for spec in PLACEHOLDER_SPECS}

# 向后兼容：旧调用点按元组遍历。
PLACEHOLDERS: tuple[str, ...] = tuple(spec.key for spec in PLACEHOLDER_SPECS)


def contains_placeholder(text: Any, placeholder: str) -> bool:
    """判断文本中是否已出现指定占位符（用于避免重复注入运行时块）。"""
    if not isinstance(text, str) or not text:
        return False
    return bool(placeholder) and placeholder in text


def find_unknown_placeholders(text: Any) -> list[str]:
    """列出模板里所有未登记的占位符，供前端提示用户拼写错误。"""
    if not isinstance(text, str) or not text:
        return []
    return sorted(
        {
            match.group(0)
            for match in _ANY_PLACEHOLDER_RE.finditer(text)
            if match.group(0) not in SPEC_BY_KEY
        }
    )


def placeholder_labels() -> dict[str, str]:
    """返回 占位符 -> 中文说明 映射，供配置页展示可用清单。"""
    return {spec.key: spec.label for spec in PLACEHOLDER_SPECS}


def _spec_available(spec: PlaceholderSpec, capabilities: dict[str, bool]) -> bool:
    """能力集是否满足该占位符的全部依赖。"""
    if not spec.requires:
        return True
    return all(bool(capabilities.get(cap, False)) for cap in spec.requires)


# 整行只是一个“标签：占位符”的独立条目，删除后才不会损失信息。
# 标签部分必须不含任何句子分隔符：否则「请询问近况；最后发言：{{x}}」
# 会被误判成独立标签行，整行删掉会连带丢弃前面的有效指令。
_STANDALONE_LABEL_RE = re.compile(
    r"^[^：:＝=；。，、！？,.;!?（）()【】\[\]]{1,16}[：:＝=]\s*"
    r"(\{\{[^{}]*\}\})\s*$"
)


def _render_line(
    line: str,
    capabilities: dict[str, bool],
    values: dict[str, str],
) -> str | None:
    """处理单行；返回 None 表示整行应被删除。"""
    present = [spec for spec in PLACEHOLDER_SPECS if spec.key in line]
    if not present:
        return line

    unavailable_removable = [
        spec
        for spec in present
        if not _spec_available(spec, capabilities) and spec.line_removable
    ]

    # 一次扫描完成替换：替换值里若恰好含有占位符字面量（例如聊天记录正文），
    # re.sub 不会对已替换内容再次匹配，避免二次改写。
    mapping: dict[str, str] = {}
    for spec in present:
        if spec.key in values:
            mapping[spec.key] = values[spec.key]
        elif _spec_available(spec, capabilities):
            # 能力满足但调用方没给值：说明本链路不负责填充，保持原样留给下个环节。
            continue
        else:
            mapping[spec.key] = spec.fallback

    if mapping:
        pattern = "|".join(re.escape(key) for key in mapping)
        rendered = re.sub(pattern, lambda m: mapping[m.group(0)], line)
    else:
        rendered = line

    if not unavailable_removable:
        return rendered

    # 只有“独立标签行”才允许整行删除。
    # 「请询问近况；最后发言：{{x}}」这类混合行删除会连带丢掉有效指令，
    # 必须保留其余内容。
    stripped_raw = _LIST_PREFIX_RE.sub("", line).strip()
    standalone = _STANDALONE_LABEL_RE.match(stripped_raw)
    removable_keys = {spec.key for spec in unavailable_removable}
    if standalone and standalone.group(1) in removable_keys:
        return None

    # 标签行的值被降级成空后，若整行只剩标点也视为无有效信息。
    stripped = _LIST_PREFIX_RE.sub("", rendered).strip()
    if not stripped or _PUNCT_ONLY_RE.match(stripped):
        return None
    return rendered


def resolve_placeholders(
    text: Any,
    values: dict[str, str] | None = None,
    capabilities: dict[str, bool] | None = None,
) -> str:
    """能力感知的占位符消解：可用则渲染，不可用则按规格降级。

    Args:
        text: 模板正文。
        values: 已准备好的占位符取值（键为占位符全称，如 "{{current_time}}"）。
        capabilities: 本次请求具备的能力集，缺省视为全部可用。

    Returns:
        消解后的文本，保证不残留任何已登记占位符字面量。
    """
    if text is None:
        return ""
    template = text if isinstance(text, str) else str(text)
    if not template:
        return ""

    caps = dict(capabilities or {})
    if not capabilities:
        # 未显式声明能力时按“全部具备”处理，保持旧调用方的行为不变。
        caps = {
            spec_key: True for spec in PLACEHOLDER_SPECS for spec_key in spec.requires
        }

    rendered_values = dict(values or {})
    lines = template.split("\n")
    kept: list[str] = []
    for line in lines:
        result = _render_line(line, caps, rendered_values)
        if result is None:
            continue
        kept.append(result)

    # 整行删除可能留下连续空行，压缩为最多一个空行，避免提示词出现大片空白。
    normalised: list[str] = []
    for line in kept:
        if not line.strip() and normalised and not normalised[-1].strip():
            continue
        normalised.append(line)
    return "\n".join(normalised).strip()


def render_placeholders(
    text: Any,
    *,
    current_time: str | None = None,
    unanswered_count: int | None = None,
    platform_history_lines: str | None = None,
    platform_history_last_time: str | None = None,
    conversation_type: str | None = None,
) -> str:
    """按给定值替换占位符（不做能力判定）。

    只扫描原模板一遍，替换进去的内容不会再次被解析。
    这样历史消息正文里出现的 {{current_time}} 字面量会原样保留，
    不会被后续步骤当成占位符二次改写。
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
    if conversation_type is not None:
        values[PLACEHOLDER_CONVERSATION_TYPE] = str(conversation_type)
    if not values:
        return template

    # 一次扫描完成全部替换；re.sub 不会对替换结果再次匹配。
    pattern = "|".join(re.escape(key) for key in values)
    return re.sub(pattern, lambda m: values[m.group(0)], template)


def build_capabilities(
    *,
    session_type: str = "",
    source_mode: str = "",
    platform_history_count: int = 0,
    conversation_history_count: int = 0,
) -> dict[str, bool]:
    """由运行时事实推导能力集。

    平台流水能力必须同时要求「模式需要流水」与「确实有记录」：
    用户可能开了 platform_message_history，但群聊刚建立时流水为空，
    此时流水相关占位符仍应判定为不可用。
    """
    has_platform = source_mode in {"platform_message_history", "hybrid"} and (
        platform_history_count > 0
    )
    has_conversation = source_mode in {"conversation_history", "hybrid"} and (
        conversation_history_count > 0
    )
    return {
        CAP_TIME: True,
        CAP_UNANSWERED: True,
        CAP_SESSION_TYPE: session_type in {"friend", "group"},
        CAP_PLATFORM_HISTORY: has_platform,
        CAP_CONVERSATION_HISTORY: has_conversation,
    }


def conversation_type_label(session_type: str) -> str:
    """会话类型的中文标签，供 {{conversation_type}} 使用。"""
    return {"friend": "私聊", "group": "群聊"}.get(session_type, "会话")


__all__ = [
    "CAP_CONVERSATION_HISTORY",
    "CAP_PLATFORM_HISTORY",
    "CAP_SESSION_TYPE",
    "CAP_TIME",
    "CAP_UNANSWERED",
    "PLACEHOLDERS",
    "PLACEHOLDER_CONVERSATION_TYPE",
    "PLACEHOLDER_CURRENT_TIME",
    "PLACEHOLDER_PLATFORM_HISTORY_LAST_TIME",
    "PLACEHOLDER_PLATFORM_HISTORY_LINES",
    "PLACEHOLDER_SPECS",
    "PLACEHOLDER_UNANSWERED_COUNT",
    "PlaceholderSpec",
    "SPEC_BY_KEY",
    "build_capabilities",
    "contains_placeholder",
    "conversation_type_label",
    "find_unknown_placeholders",
    "placeholder_labels",
    "render_placeholders",
    "resolve_placeholders",
]
