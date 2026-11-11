"""提示词模板的纯数据工具。

模板统一存放在 session_defaults.prompt_settings.templates 下，每条模板就是一条
「主动消息正文」候选：说什么由模板决定，怎么输出由 prompt_settings.output_format
决定，两者互不干扰。

- scope 为空     -> 私聊、群聊都生效
- scope="group"  -> 仅群聊生效（迁移期保留的旧群聊自定义模板）
- scope="friend" -> 仅私聊生效

输出格式只保留两个预设：插件默认 / 用户自定义。它是单值设置而不是模板列表，
因为格式偏好必须稳定：把风格丢进权重抽签会让同一会话的格式随机漂移，而
「只输出一条」和「带旁白多段」是互斥的长期偏好，不该每轮重掷。
"""

from __future__ import annotations

import math
import random
from typing import Any

from .config_keys import (
    OUTPUT_FORMAT_CUSTOM_KEY,
    OUTPUT_FORMAT_KEY,
    PROMPT_SETTINGS_KEY,
)

# 配置键与默认值集中定义，避免各层重复硬编码。
TEMPLATES_KEY = "templates"
DEFAULT_TEMPLATE_WEIGHT = 100
# 迁移器生成的模板使用固定 id，用于与用户自建模板区分。
MIGRATED_TEMPLATE_ID = "legacy"

# AstrBot 的 template_list 控件要求每个条目用 __template_key 指向模板定义：
# 缺少该字段时管理面板会显示「未指定模板 / 找不到对应模板」，保存也会被校验拒绝。
# 本插件只提供一种模板类型，因此键名固定。
TEMPLATE_KEY_FIELD = "__template_key"
DEFAULT_TEMPLATE_KEY = "prompt_template"

# 输出格式的两个预设。default 使用插件内置风格约束，custom 使用用户正文。
OUTPUT_FORMAT_DEFAULT = "default"
OUTPUT_FORMAT_CUSTOM = "custom"

# scope 取值
SCOPE_ANY = ""
SCOPE_FRIEND = "friend"
SCOPE_GROUP = "group"

_SCOPE_ALIASES: dict[str, str] = {
    "": SCOPE_ANY,
    "all": SCOPE_ANY,
    "any": SCOPE_ANY,
    "通用": SCOPE_ANY,
    "全部": SCOPE_ANY,
    "friend": SCOPE_FRIEND,
    "private": SCOPE_FRIEND,
    "私聊": SCOPE_FRIEND,
    "group": SCOPE_GROUP,
    "群聊": SCOPE_GROUP,
}

# 旧版 role 值的语义归类，仅供迁移器识别历史配置使用。
# 运行期不再有 role 概念：模板一律是主动消息正文。
LEGACY_CONTEXT_ROLES: frozenset[str] = frozenset(
    {"上下文注入", "平台流水", "流水", "context", "history", "platform_history"}
)
LEGACY_STYLE_ROLES: frozenset[str] = frozenset(
    {"输出风格", "风格", "格式要求", "style", "output_style"}
)


def legacy_role_kind(role: Any) -> str:
    """把旧 role 归为 main / context / style 三类，供迁移分流。"""
    text = _as_text(role)
    if not text:
        return "main"
    lowered = text.lower()
    if text in LEGACY_CONTEXT_ROLES or lowered in LEGACY_CONTEXT_ROLES:
        return "context"
    if text in LEGACY_STYLE_ROLES or lowered in LEGACY_STYLE_ROLES:
        return "style"
    # 无法识别时按主提示词处理：宁可多留一条正文，也不要丢掉用户内容。
    return "main"


def normalize_scope(scope: Any) -> str:
    """归一化 scope；未知值按通用处理（不限制会话类型）。"""
    text = _as_text(scope)
    return _SCOPE_ALIASES.get(text, _SCOPE_ALIASES.get(text.lower(), SCOPE_ANY))


def scope_matches(template: dict[str, Any], session_type: str) -> bool:
    """判断模板是否适用于给定会话类型。"""
    scope = normalize_scope(template.get("scope"))
    if scope == SCOPE_ANY:
        return True
    return scope == session_type


def normalize_template(raw: Any) -> dict[str, Any] | None:
    """把单个模板归一化为 {id, name, text, enabled, weight, scope}。

    另外保留 __template_key：AstrBot 管理面板靠它匹配模板定义，
    条目缺失该字段时会显示「未指定模板」并拒绝整份配置保存。

    只有完全无法解释的输入才返回 None。正文为空的条目会被保留
    （宽容原则：归一化结果会被写回配置，丢弃等于永久删除用户数据），
    空正文条目在选取阶段由 pick_template 跳过。
    """
    if isinstance(raw, str):
        return {
            "id": "",
            "name": "",
            "text": raw,
            "enabled": True,
            "weight": DEFAULT_TEMPLATE_WEIGHT,
            "scope": SCOPE_ANY,
            TEMPLATE_KEY_FIELD: DEFAULT_TEMPLATE_KEY,
        }

    if not isinstance(raw, dict):
        return None

    text = raw.get("text")
    if not isinstance(text, str):
        text = "" if text is None else str(text)

    template: dict[str, Any] = {
        "id": _as_text(raw.get("id")),
        # name 是给用户看的用途标签，例如“延续话题”“开辟新话题”。
        "name": _as_text(raw.get("name")),
        "text": text,
        "enabled": _as_bool(raw.get("enabled", True)),
        "weight": _as_weight(raw.get("weight", DEFAULT_TEMPLATE_WEIGHT)),
        "scope": normalize_scope(raw.get("scope")),
    }

    # 列表项不参与配置完整性裁剪，可以安全携带扩展字段。
    # 唯一例外是已废除的 role：它曾用来区分注入槽位，现在模板只表达主动消息正文，
    # 继续携带只会让配置里留着一个永远不生效的键，因此显式跳过。
    for key, value in raw.items():
        if key not in template and key != "role":
            template[key] = value
    # 旧配置没有该字段，统一补上才能被 AstrBot 管理面板识别。
    template.setdefault(TEMPLATE_KEY_FIELD, DEFAULT_TEMPLATE_KEY)
    return template


def normalize_templates(value: Any) -> list[dict[str, Any]]:
    """归一化整个模板列表，保持原顺序并丢弃非法项。"""
    if not isinstance(value, list):
        return []

    result: list[dict[str, Any]] = []
    for item in value:
        template = normalize_template(item)
        if template is not None:
            result.append(template)
    return result


def _prompt_settings(section: Any) -> dict[str, Any]:
    """取出 prompt_settings 段；缺失时退回 section 自身以兼容旧覆写形态。"""
    if not isinstance(section, dict):
        return {}
    settings = section.get(PROMPT_SETTINGS_KEY)
    if isinstance(settings, dict):
        return settings
    return section


def get_templates(section: Any) -> list[dict[str, Any]]:
    """从配置段读取模板列表（统一入口，兼容旧位置）。"""
    if not isinstance(section, dict):
        return []

    settings = section.get(PROMPT_SETTINGS_KEY)
    if isinstance(settings, dict):
        templates = settings.get(TEMPLATES_KEY)
        if isinstance(templates, list):
            return normalize_templates(templates)

    # 兼容会话覆写里直接写 templates 的形态。
    templates = section.get(TEMPLATES_KEY)
    if isinstance(templates, list):
        return normalize_templates(templates)

    return []


def enabled_templates(templates: Any) -> list[dict[str, Any]]:
    """返回全部启用中的模板（保持配置顺序）。"""
    return [item for item in normalize_templates(templates) if item["enabled"]]


def pick_template(
    templates: Any,
    *,
    weighted: bool = False,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """从给定的启用模板里选出一条。

    weighted=False 时固定取第一条，结果确定、便于排查；
    weighted=True 时按 weight 加权随机，权重越大被选中概率越高。
    """
    # 空正文条目不可用于生成，必须在选取阶段排除。
    candidates = [item for item in enabled_templates(templates) if item["text"].strip()]
    if not candidates:
        return None
    if not weighted or len(candidates) == 1:
        return candidates[0]

    picker = rng or random
    weights = [float(item["weight"]) for item in candidates]
    total = sum(weights)
    if total <= 0:
        # 权重异常时退化为均匀随机，保证永远能选出模板。
        return candidates[picker.randrange(len(candidates))]

    threshold = picker.random() * total
    accumulated = 0.0
    for item, weight in zip(candidates, weights):
        accumulated += weight
        if threshold < accumulated:
            return item
    return candidates[-1]


def _is_usable_template(item: dict[str, Any]) -> bool:
    """判断模板能否当作主动消息正文使用。

    旧版本的流水模板正文里含 {{platform_history_lines}}，它是一段「包裹聊天
    记录」的外壳，不是开场白。迁移后残留的这类模板如果被当成正文注入，
    模型会收到一份嵌套的记录格式说明，直接导致说话方式崩坏。
    因此运行时再加一道兜底：含流水载荷占位符的模板一律不参与正文选取。
    """
    text = str(item.get("text") or "")
    if not text.strip():
        return False
    return "{{platform_history_lines}}" not in text


def resolve_active_template(
    section: Any,
    *,
    session_type: str = "",
    weighted: bool = False,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """解析当前生效的主动消息模板（含 id / name / weight）。

    按 scope → 通用 的顺序挑选：先找仅本会话类型生效的模板，
    再退回通用模板。这样迁移期保留的旧群聊模板不会污染私聊。
    """
    if not isinstance(section, dict):
        return None

    candidates = [
        item
        for item in enabled_templates(get_templates(section))
        if scope_matches(item, session_type) and _is_usable_template(item)
    ]
    if not candidates:
        return None

    # 先尝试精确匹配当前会话类型的模板。
    if session_type:
        scoped = [
            item
            for item in candidates
            if normalize_scope(item.get("scope")) == session_type
        ]
        if scoped:
            return pick_template(scoped, weighted=weighted, rng=rng)

    generic = [
        item for item in candidates if normalize_scope(item.get("scope")) == SCOPE_ANY
    ]
    return pick_template(generic, weighted=weighted, rng=rng)


def resolve_active_prompt(
    section: Any,
    *,
    session_type: str = "",
    weighted: bool = False,
    rng: random.Random | None = None,
) -> str:
    """解析当前生效的提示词正文；无可用提示词时返回空串。"""
    template = resolve_active_template(
        section,
        session_type=session_type,
        weighted=weighted,
        rng=rng,
    )
    return str(template["text"]) if template else ""


def normalize_output_format(value: Any) -> str:
    """把输出格式配置归一化为 default / custom；未知值回退默认。"""
    text = _as_text(value).lower()
    return (
        OUTPUT_FORMAT_CUSTOM if text == OUTPUT_FORMAT_CUSTOM else OUTPUT_FORMAT_DEFAULT
    )


def get_output_format(section: Any) -> tuple[str, str]:
    """返回 (模式, 自定义格式正文)。

    选了「自定义」但正文为空时回退默认：否则风格约束会整段消失，
    模型可能开始输出旁白或多段候选，用户却看不出原因。
    """
    settings = _prompt_settings(section)
    mode = normalize_output_format(settings.get(OUTPUT_FORMAT_KEY))
    custom = settings.get(OUTPUT_FORMAT_CUSTOM_KEY)
    text = custom.strip() if isinstance(custom, str) else ""
    if mode == OUTPUT_FORMAT_CUSTOM and not text:
        return OUTPUT_FORMAT_DEFAULT, ""
    return mode, text


def session_type_of(section: Any) -> str:
    """从生效配置里读取会话类型（config_service 写入的 _session_type）。"""
    if not isinstance(section, dict):
        return ""
    return str(section.get("_session_type") or "")


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _as_weight(value: Any) -> int:
    # bool 是 int 的子类，必须先排除，避免 True 被当成权重 1。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TEMPLATE_WEIGHT
    # JSON 允许 NaN / Infinity，int() 转换会抛异常，需先排除非有限值。
    if not math.isfinite(value):
        return DEFAULT_TEMPLATE_WEIGHT
    weight = int(value)
    return weight if weight > 0 else DEFAULT_TEMPLATE_WEIGHT


__all__ = [
    "DEFAULT_TEMPLATE_KEY",
    "DEFAULT_TEMPLATE_WEIGHT",
    "LEGACY_CONTEXT_ROLES",
    "LEGACY_STYLE_ROLES",
    "MIGRATED_TEMPLATE_ID",
    "OUTPUT_FORMAT_CUSTOM",
    "OUTPUT_FORMAT_DEFAULT",
    "PROMPT_SETTINGS_KEY",
    "SCOPE_ANY",
    "SCOPE_FRIEND",
    "SCOPE_GROUP",
    "TEMPLATES_KEY",
    "TEMPLATE_KEY_FIELD",
    "enabled_templates",
    "get_output_format",
    "get_templates",
    "legacy_role_kind",
    "normalize_output_format",
    "normalize_scope",
    "normalize_template",
    "normalize_templates",
    "pick_template",
    "resolve_active_prompt",
    "resolve_active_template",
    "scope_matches",
    "session_type_of",
]
