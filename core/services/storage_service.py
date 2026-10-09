"""会话持久化服务。

由旧 core/data_storage.py 等价搬迁而来，读写与清理逻辑保持不变。
本服务在 SessionStore 之上提供会话键规范化与无效数据清理。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..store.session_store import SessionStore


class StorageService:
    """会话数据加载/保存与迁移清理服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._store = SessionStore(plugin.data_dir)
        # 复用容器中已存在的文件路径，保持完全一致的落盘位置。
        if getattr(plugin, "session_data_file", None):
            self._store.session_data_file = plugin.session_data_file

    async def load_data(self) -> None:
        """从文件中加载会话数据（异步无锁内部实现）。"""
        self.plugin.session_data = await self._store.load()

    async def save_data(self) -> None:
        """将会话数据保存到文件（异步无锁内部实现）。"""
        await self._store.save(self.plugin.session_data)

    def merge_session_info(self, base: dict, incoming: dict) -> dict:
        """转发到 SessionStore 的会话数据合并实现。"""
        return self._store.merge_session_info(base, incoming)

    def normalize_session_data(self) -> bool:
        """规范化并合并 session_data 中的重复会话键。

        相同规范键的载荷按 SessionStore 的合并策略归并，返回是否有变更。
        """
        plugin = self.plugin
        if not plugin.session_data:
            return False

        normalized_data: dict[str, dict] = {}
        changed = False

        for session_id, payload in list(plugin.session_data.items()):
            normalized_id = plugin.session_service.normalize_session_id(session_id)
            if normalized_id != session_id:
                changed = True
                logger.info(
                    f"[主动消息] 规范化会话键: {plugin.session_service.get_session_log_str(session_id)} -> {normalized_id}"
                )

            existing = normalized_data.get(normalized_id)
            if existing:
                normalized_data[normalized_id] = self._store.merge_session_info(
                    existing, payload
                )
                changed = True
            else:
                normalized_data[normalized_id] = payload

        if changed:
            plugin.session_data = normalized_data

        return changed

    def cleanup_invalid_session_data(self) -> int:
        """清理无效的会话数据（旧格式遗留或不可解析条目）。

        判定标准：以旧前缀开头，或无法被 session_service 解析为合法 UMO。
        返回被清理的条目数。
        """
        plugin = self.plugin
        cleaned_count = 0
        invalid_sessions: list[str] = []

        for session_id in list(plugin.session_data.keys()):
            parsed = plugin.session_service.parse_session_id(session_id)
            if (
                session_id.startswith("friend_message:")
                or session_id.startswith("group_message:")
                or not parsed
            ):
                invalid_sessions.append(session_id)
                cleaned_count += 1

        for session_id in invalid_sessions:
            del plugin.session_data[session_id]
            logger.info(
                f"[主动消息] 清理了无效的会话数据: {plugin.session_service.get_session_log_str(session_id)}"
            )

        return cleaned_count
