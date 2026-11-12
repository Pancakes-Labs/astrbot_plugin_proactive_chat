"""会话持久化服务。

由旧 core/data_storage.py 等价搬迁而来，读写与清理逻辑保持不变。
本服务在 SessionStore 之上提供会话数据加载、保存与无效条目清理。
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

    def cleanup_invalid_session_data(self) -> int:
        """清理无效的会话数据（只剩无法解析为合法 UMO 的条目）。

        返回被清理的条目数。
        """
        plugin = self.plugin
        invalid_sessions = [
            session_id
            for session_id in list(plugin.session_data.keys())
            if not plugin.session_service.parse_session_id(session_id)
        ]

        for session_id in invalid_sessions:
            del plugin.session_data[session_id]
            logger.info(
                f"[主动消息] 清理了无效的会话数据: {plugin.session_service.get_session_log_str(session_id)}"
            )

        return len(invalid_sessions)
