"""会话数据持久化与键规范化。

由旧 core/data_storage.py 等价搬迁而来。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import aiofiles.os as aio_os

from astrbot.api import logger

from .session_data_file import (
    SOURCE_BACKUP,
    SOURCE_EMPTY,
    atomic_write_json,
    read_json_with_recovery,
)


class SessionStore:
    """负责 session_data.json 的读写、键规范化与无效数据清理。"""

    def __init__(self, data_dir: Any, filename: str = "session_data.json") -> None:
        self.data_dir = data_dir
        self.session_data_file = data_dir / filename
        # 加载期发现的损坏说明，由 StorageService 取走并落日志。
        self.last_load_notice = ""

    async def load(self) -> dict:
        """从文件加载会话数据，主文件损坏时自动从备份回滚。"""
        data, source, notice = await read_json_with_recovery(self.session_data_file)
        self.last_load_notice = ""

        if source == SOURCE_BACKUP:
            # 备份可用：先把损坏的主文件留档，再用备份重建主文件。
            logger.error(
                f"[主动消息] 会话数据文件已损坏（{notice}），已自动从备份恢复喵。"
            )
            self.last_load_notice = f"从备份恢复（主文件损坏: {notice}）"
            await self._archive_corrupted()
            try:
                await atomic_write_json(self.session_data_file, data, keep_backup=False)
            except OSError as e:
                logger.error(f"[主动消息] 用备份重建会话数据文件失败喵: {e}")
        elif source == SOURCE_EMPTY and notice:
            logger.error(
                f"[主动消息] 会话数据与备份均不可用（{notice}），将以空数据启动喵。"
            )
            self.last_load_notice = f"主备均不可用（{notice}）"

        return data

    async def _archive_corrupted(self) -> None:
        """把损坏的主文件改名留档，避免后续写入直接覆盖证据。"""
        target = Path(str(self.session_data_file) + ".corrupted")
        try:
            if await aio_os.path.exists(self.session_data_file):
                os.replace(self.session_data_file, target)
                logger.warning(
                    f"[主动消息] 已把损坏的会话数据文件留档为 {target.name} 喵。"
                )
        except OSError as e:
            logger.warning(f"[主动消息] 留存损坏会话数据文件失败喵: {e}")

    async def save(self, session_data: dict) -> None:
        """将会话数据原子写入文件。

        先写临时文件再整体替换，避免进程被强杀时留下半截 JSON；
        替换前把旧文件转为 .bak，作为下次启动的回滚点。
        """
        try:
            await atomic_write_json(self.session_data_file, session_data)
        except OSError as e:
            logger.error(f"[主动消息] 保存会话数据失败喵: {e}")
