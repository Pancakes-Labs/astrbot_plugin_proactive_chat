"""会话持久化服务。

由旧 core/data_storage.py 等价搬迁而来，读写与清理逻辑保持不变。
本服务在 SessionStore 之上提供会话数据加载、保存与无效条目清理。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..domain.unanswered_counter import UnansweredState, resolve_unanswered_state
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
        """从文件中加载会话数据（异步无锁内部实现）。

        主文件损坏时 SessionStore 会自动回退备份；这里负责把恢复结果
        与条目数记录下来，便于启动后核对数据是否凭空变少。
        """
        self.plugin.session_data = await self._store.load()

        # 只在主文件损坏 / 主备均不可用这两种真实故障时告警，
        # 正常启动与非规范键的历史数据不会走到这里。
        notice = getattr(self._store, "last_load_notice", "")
        if notice:
            logger.error(f"[主动消息] 会话数据加载异常喵: {notice}")
        logger.info(
            f"[主动消息] 会话数据加载完成，共 {len(self.plugin.session_data)} 条记录喵。"
        )
        await self._persist_initial_backup()

    async def _persist_initial_backup(self) -> None:
        """启动加载完成后固化一份备份。

        此时内存数据已确认可用，用它重建备份可以修复「主文件好、备份坏」
        或「备份落后于主文件」的情况，保证下次强杀时总有可靠回滚点。
        """
        try:
            if not self.plugin.session_data:
                return
            await self._store.save(self.plugin.session_data)
        except Exception as e:
            logger.warning(f"[主动消息] 启动期写入会话数据备份失败喵: {e}")

    def get_unanswered_state(self, session_id: str) -> UnansweredState:
        """读取会话的未回复计数状态，区分「缺失」与「为 0」。"""
        session_info = self.plugin.session_data.get(session_id)
        return resolve_unanswered_state(session_info)

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
