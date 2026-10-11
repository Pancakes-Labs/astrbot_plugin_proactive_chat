"""AstrBot 平台实例解析适配器。

本模块是全项目访问 AstrBot 平台实例的唯一入口，集中收敛：
- 平台实例列表获取（兼容 get_insts / platform_insts 两种 API）
- “可用 IM 平台”过滤规则（排除 webchat 与无 id 的占位实例）
- 按 ID / 显示名解析平台实例
"""

from __future__ import annotations

from typing import Any


def _iter_platform_instances(platform_manager: Any) -> list[Any]:
    """尽力获取平台实例列表（兼容 get_insts / platform_insts 两种 API）。"""
    if platform_manager is None:
        return []

    try:
        platforms = platform_manager.get_insts()
    except Exception:
        try:
            platforms = platform_manager.platform_insts
        except Exception:
            return []

    if not platforms:
        return []
    return list(platforms)


def is_active_platform_instance(inst: Any) -> bool:
    """判断平台实例是否为可用的 IM 平台（排除 webchat 与无 id 的占位实例）。"""
    try:
        platform_id = inst.meta().id
    except Exception:
        return False
    return bool(platform_id) and "webchat" not in str(platform_id).lower()


def iter_active_platform_instances(platform_manager: Any) -> list[Any]:
    """列出所有可用的 IM 平台实例（排除 webchat）。"""
    return [
        inst
        for inst in _iter_platform_instances(platform_manager)
        if is_active_platform_instance(inst)
    ]


def find_platform_by_id(platform_manager: Any, platform_id: str) -> Any:
    """按平台 ID 精确查找平台实例（不匹配显示名）。"""
    if not platform_id:
        return None

    for inst in _iter_platform_instances(platform_manager):
        try:
            if inst.meta().id == platform_id:
                return inst
        except Exception:
            continue

    return None


def resolve_platform_instance(
    platform_manager: Any,
    platform_id: str,
) -> Any:
    """按平台 ID 解析平台实例，匹配不到时退回按平台显示名匹配。

    同时兼容 meta().id 与 meta().name 两种标识方式。
    """
    if not platform_id:
        return None

    platforms = _iter_platform_instances(platform_manager)
    if not platforms:
        return None

    for inst in platforms:
        try:
            if inst.meta().id == platform_id:
                return inst
        except Exception:
            continue

    for inst in platforms:
        try:
            if inst.meta().name == platform_id:
                return inst
        except Exception:
            continue

    return None
