"""配置迁移器。

只做一次性、幂等的结构升级，不参与运行时业务逻辑。
当前迁移项：旧版单一字符串 proactive_prompt -> prompt_templates 列表。
"""

from __future__ import annotations

import copy
from typing import Any

from astrbot.api import logger

from ..domain.prompt_templates import (
    LEGACY_PROMPT_KEY,
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

    # 已经是列表结构：只做归一化清洗，补齐 enabled / weight。
    if isinstance(existing, list):
        normalized = normalize_templates(existing)
        if normalized != existing:
            section[PROMPT_TEMPLATES_KEY] = normalized
            return True
        return False

    # 尚未迁移且存在旧字符串：转成单条模板，同时保留旧字段以便回滚。
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


def migrate_config(config: Any) -> bool:
    """就地迁移全局配置，返回是否发生变更。"""
    if not isinstance(config, dict):
        return False

    changed = False
    for section_name in SESSION_SETTINGS_KEYS:
        if _migrate_section(config.get(section_name), section_name):
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
            await store.set_override(session_id, patched)
            changed_sessions += 1
        except Exception as e:
            logger.warning(f"[主动消息] 迁移会话 {session_id} 的覆写配置失败喵: {e}")

    return changed_sessions


__all__ = ["migrate_config", "migrate_override_store", "SESSION_SETTINGS_KEYS"]
