"""提示词模板的纯数据工具。

对应 issue #45「多提示词模板注入」的基础结构预留：
配置为若干条模板，每条专注一件事，可单独启停并设置注入权重，
每轮主动消息按权重抽取一条使用（不拼接多条）。

生效优先级（保迁移前后完全一致，用户无感）：
1. 用户自己创建的模板（id 不为 MIGRATED_TEMPLATE_ID）中启用项；
2. 旧字段 proactive_prompt（保持旧版可编辑性，改了立刻生效）;
3. 迁移自动生成的模板（id 为 MIGRATED_TEMPLATE_ID）。

这样自动迁移只是把旧文本复制一份留档，不会抢走旧字段的控制权；
等后续 WebUI 真正编辑模板时，新模板自成一组并优先生效。
"""

from __future__ import annotations

import random
from typing import Any

# 配置键与默认值集中定义，避免各层重复硬编码。
PROMPT_TEMPLATES_KEY = "prompt_templates"
LEGACY_PROMPT_KEY = "proactive_prompt"
DEFAULT_TEMPLATE_WEIGHT = 100
DEFAULT_TEMPLATE_ROLE = "task"
# 迁移器生成的模板使用固定 id，用于与用户自建模板区分优先级。
MIGRATED_TEMPLATE_ID = "legacy"


def normalize_template(raw: Any) -> dict[str, Any] | None:
    """把单个模板归一化为 {id, name, role, text, enabled, weight}。

    没有有效正文的项返回 None，避免脏数据进入运行时。
    """
    if isinstance(raw, str):
        if not raw.strip():
            return None
        return {
            "id": "",
            "name": "",
            "role": DEFAULT_TEMPLATE_ROLE,
            "text": raw,
            "enabled": True,
            "weight": DEFAULT_TEMPLATE_WEIGHT,
        }

    if not isinstance(raw, dict):
        return None

    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    template: dict[str, Any] = {
        "id": _as_text(raw.get("id")),
        # name 是给用户看的用途标签，例如“延续话题”“开辟新话题”。
        "name": _as_text(raw.get("name")),
        "role": _as_text(raw.get("role")) or DEFAULT_TEMPLATE_ROLE,
        "text": text,
        "enabled": _as_bool(raw.get("enabled", True)),
        "weight": _as_weight(raw.get("weight", DEFAULT_TEMPLATE_WEIGHT)),
    }

    # 列表项不参与配置完整性裁剪，可以安全携带扩展字段。
    for key, value in raw.items():
        if key not in template:
            template[key] = value
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


def build_templates_from_legacy(text: Any) -> list[dict[str, Any]]:
    """把旧版单一的 proactive_prompt 转成只含一条留档模板的列表。"""
    if not isinstance(text, str) or not text.strip():
        return []
    return [
        {
            "id": MIGRATED_TEMPLATE_ID,
            "name": "默认提示词",
            "role": DEFAULT_TEMPLATE_ROLE,
            "text": text,
            "enabled": True,
            "weight": DEFAULT_TEMPLATE_WEIGHT,
        }
    ]


def enabled_templates(templates: Any) -> list[dict[str, Any]]:
    """返回全部启用中的模板（保持配置顺序）。"""
    return [item for item in normalize_templates(templates) if item["enabled"]]


def user_templates(templates: Any) -> list[dict[str, Any]]:
    """返回用户自建的启用模板，排除迁移器生成的留档项。"""
    return [
        item
        for item in enabled_templates(templates)
        if item["id"] != MIGRATED_TEMPLATE_ID
    ]


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
    candidates = enabled_templates(templates)
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


def resolve_active_template(
    section: Any,
    *,
    weighted: bool = False,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """解析当前生效模板的完整结构（含 id / name / role / weight）。

    优先级见模块说明：用户模板 > 旧字段 > 迁移留档模板。
    返回 None 表示该配置段没有任何可用提示词。
    """
    if not isinstance(section, dict):
        return None

    raw_templates = section.get(PROMPT_TEMPLATES_KEY)

    picked = pick_template(user_templates(raw_templates), weighted=weighted, rng=rng)
    if picked is not None:
        return picked

    # 旧字段优先于迁移留档，保证用户在面板里改 proactive_prompt 依然立即生效。
    legacy = section.get(LEGACY_PROMPT_KEY)
    if isinstance(legacy, str) and legacy.strip():
        return {
            "id": "",
            "name": "",
            "role": DEFAULT_TEMPLATE_ROLE,
            "text": legacy,
            "enabled": True,
            "weight": DEFAULT_TEMPLATE_WEIGHT,
        }

    return pick_template(
        [
            item
            for item in enabled_templates(raw_templates)
            if item["id"] == MIGRATED_TEMPLATE_ID
        ],
        weighted=weighted,
        rng=rng,
    )


def resolve_active_prompt(
    section: Any,
    *,
    weighted: bool = False,
    rng: random.Random | None = None,
) -> str:
    """解析当前生效的提示词正文；无可用提示词时返回空串。"""
    template = resolve_active_template(section, weighted=weighted, rng=rng)
    return str(template["text"]) if template else ""


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
    weight = int(value)
    return weight if weight > 0 else DEFAULT_TEMPLATE_WEIGHT


__all__ = [
    "DEFAULT_TEMPLATE_ROLE",
    "DEFAULT_TEMPLATE_WEIGHT",
    "LEGACY_PROMPT_KEY",
    "MIGRATED_TEMPLATE_ID",
    "PROMPT_TEMPLATES_KEY",
    "build_templates_from_legacy",
    "enabled_templates",
    "normalize_template",
    "normalize_templates",
    "pick_template",
    "resolve_active_prompt",
    "resolve_active_template",
    "user_templates",
]
