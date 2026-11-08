"""配置迁移器。

只做一次性、幂等的结构升级，不参与运行时业务逻辑。
当前迁移项：
1. 旧版单一字符串 proactive_prompt -> prompt_templates 列表；
2. 平台流水提示词由历史默认值升级为新默认值（仅逐字相同时才动）。
"""

from __future__ import annotations

import copy
from typing import Any

from astrbot.api import logger

from ..domain.prompt_templates import (
    LEGACY_PLATFORM_HISTORY_PROMPTS,
    LEGACY_PROMPT_KEY,
    PLATFORM_HISTORY_PROMPT_KEY,
    PROMPT_TEMPLATES_KEY,
    build_templates_from_legacy,
    normalize_templates,
)

# 需要迁移的配置分组：私聊与群聊各一份，结构完全对称。
SESSION_SETTINGS_KEYS = ("friend_settings", "group_settings")


def _migrate_section(section: Any, section_name: str) -> bool:
    """迁移单个配置分组，返回是否发生了变更。"""
    if not isinstance(section, dict):
        return False

    existing = section.get(PROMPT_TEMPLATES_KEY)
    legacy = section.get(LEGACY_PROMPT_KEY)

    if isinstance(existing, list) and existing:
        # 已有模板内容：只做归一化清洗，补齐缺省字段，不动已有条目。
        normalized = normalize_templates(existing)
        if normalized != existing:
            section[PROMPT_TEMPLATES_KEY] = normalized
            return True
        return False

    # 空列表或缺失：都视为尚未迁移。
    templates = build_templates_from_legacy(legacy)
    if not templates:
        return False

    section[PROMPT_TEMPLATES_KEY] = templates
    logger.info(
        f"[主动消息] 已将 {section_name}.{LEGACY_PROMPT_KEY} 迁移为 "
        f"{PROMPT_TEMPLATES_KEY}（{len(templates)} 条模板）喵。"
    )
    return True


def _migrate_override(override: Any, session_id: str) -> dict[str, Any] | None:
    """迁移单个会话覆写补丁，无变更时返回 None。"""
    if not isinstance(override, dict):
        return None

    # 覆写是按需补丁：只在用户确实覆写了提示词时才需要迁移。
    if PROMPT_TEMPLATES_KEY in override:
        normalized = normalize_templates(override.get(PROMPT_TEMPLATES_KEY))
        if normalized == override.get(PROMPT_TEMPLATES_KEY):
            return None
        patched = copy.deepcopy(override)
        patched[PROMPT_TEMPLATES_KEY] = normalized
        return patched

    templates = build_templates_from_legacy(override.get(LEGACY_PROMPT_KEY))
    if not templates:
        return None

    patched = copy.deepcopy(override)
    patched[PROMPT_TEMPLATES_KEY] = templates
    logger.info(
        f"[主动消息] 已迁移会话 {session_id} 的提示词为 {PROMPT_TEMPLATES_KEY} 喵。"
    )
    return patched


def _migrate_platform_history_prompt(
    section: Any,
    section_name: str,
    new_default: str,
) -> bool:
    """把仍是历史默认值的平台流水提示词升级为新默认值。

    配置面板会把默认值原样落盘，导致「从没改过」与「自己写了一份」
    在配置里无法区分，因此这里只做逐字比对：只要用户动过任何一个字，
    就原样保留，绝不覆盖用户内容。
    """
    if not isinstance(section, dict):
        return False

    context_settings = section.get("context_settings")
    if not isinstance(context_settings, dict):
        return False

    current = context_settings.get(PLATFORM_HISTORY_PROMPT_KEY)
    if not isinstance(current, str) or not current.strip():
        # 空值表示跟随代码默认，无需写回配置。
        return False

    legacy = LEGACY_PLATFORM_HISTORY_PROMPTS.get(section_name)
    if not legacy or current != legacy:
        return False

    context_settings[PLATFORM_HISTORY_PROMPT_KEY] = new_default
    logger.info(
        f"[主动消息] 已将 {section_name} 的平台流水提示词从历史默认值升级为新版本喵。"
    )
    return True


def migrate_config(config: Any) -> bool:
    """就地迁移全局配置，返回是否发生变更。"""
    if not isinstance(config, dict):
        return False

    # 延迟导入：context_service 依赖链较重，迁移器保持轻量。
    from .context_service import DEFAULT_PLATFORM_HISTORY_PROMPT

    changed = False
    for section_name in SESSION_SETTINGS_KEYS:
        section = config.get(section_name)
        if _migrate_section(section, section_name):
            changed = True
        if _migrate_platform_history_prompt(
            section, section_name, DEFAULT_PLATFORM_HISTORY_PROMPT
        ):
            changed = True
    return changed


async def migrate_override_store(store: Any) -> int:
    """迁移全部会话覆写，返回被改动的会话数量。"""
    if store is None:
        return 0

    list_sessions = getattr(store, "list_sessions", None)
    if not callable(list_sessions):
        return 0

    changed_sessions = 0
    try:
        session_ids = list(list_sessions())
    except Exception as e:
        logger.debug(f"[主动消息] 读取会话覆写列表失败，跳过迁移喵: {e}")
        return 0

    for session_id in session_ids:
        try:
            current = store.get_override(session_id)
            patched = _migrate_override(current, session_id)
            if patched is None:
                continue
            # 只统计真正落盘成功的会话。
            if not await store.set_override(session_id, patched):
                logger.warning(
                    f"[主动消息] 迁移会话 {session_id} 的差异配置时落盘失败喵。"
                )
                continue
            changed_sessions += 1
        except Exception as e:
            logger.warning(f"[主动消息] 迁移会话 {session_id} 的覆写配置失败喵: {e}")

    return changed_sessions


__all__ = ["migrate_config", "migrate_override_store", "SESSION_SETTINGS_KEYS"]
