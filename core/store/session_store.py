"""会话数据持久化与键规范化。

由旧 core/data_storage.py 等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiofiles
import aiofiles.os as aio_os

from astrbot.api import logger


class SessionStore:
    """负责 session_data.json 的读写、键规范化与无效数据清理。"""

    def __init__(self, data_dir: Any, filename: str = "session_data.json") -> None:
        self.data_dir = data_dir
        self.session_data_file = data_dir / filename

    async def load(self) -> dict:
        """从文件加载会话数据。"""
        if await aio_os.path.exists(self.session_data_file):
            try:
                async with aiofiles.open(self.session_data_file, encoding="utf-8") as f:
                    content = await f.read()
                    loaded_data = await asyncio.to_thread(json.loads, content)
                    if isinstance(loaded_data, dict):
                        return loaded_data
                    logger.warning(
                        "[主动消息] 会话数据文件结构无效喵，根节点应为对象，将使用空数据启动喵。"
                    )
                    return {}
            except (OSError, json.JSONDecodeError) as e:
                logger.error(
                    f"[主动消息] 加载会话数据失败喵: {e}，将使用空数据启动喵。"
                )
                return {}
        return {}

    async def save(self, session_data: dict) -> None:
        """将会话数据保存到文件。"""
        try:
            await aio_os.makedirs(self.data_dir, exist_ok=True)
            async with aiofiles.open(
                self.session_data_file, "w", encoding="utf-8"
            ) as f:
                content_to_write = await asyncio.to_thread(
                    json.dumps, session_data, indent=4, ensure_ascii=False
                )
                await f.write(content_to_write)
        except OSError as e:
            logger.error(f"[主动消息] 保存会话数据失败喵: {e}")
