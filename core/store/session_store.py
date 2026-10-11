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

    # ------------------------------------------------------------------
    # 合并与规范化（保留旧逻辑以兼容历史数据）
    # ------------------------------------------------------------------
    @staticmethod
    def merge_session_info(base: dict, incoming: dict) -> dict:
        """合并两份会话数据，避免重复任务与计数错乱。

        不同字段采用不同合并策略。计数类字段取较大值，避免已发送次数被回退。
        时间类字段取较晚值，保证调度语义向前推进。
        间隔三元组以 last_scheduled_at 较晚的一方为准。
        """
        merged = base.copy()
        for key in [
            "self_id",
            "last_message_time",
            "unanswered_count",
            "next_trigger_time",
            "last_scheduled_at",
            "last_schedule_min_interval_seconds",
            "last_schedule_max_interval_seconds",
            "last_schedule_random_interval_seconds",
        ]:
            if key not in incoming:
                continue
            if key not in merged:
                merged[key] = incoming[key]
                continue

            # 未回复计数取较大值，防止计数被历史数据覆盖变小。
            if key == "unanswered_count":
                if isinstance(merged[key], (int, float)) and isinstance(
                    incoming[key], (int, float)
                ):
                    merged[key] = max(merged[key], incoming[key])
                continue

            # 时间字段取较晚者，确保调度时序单调向前。
            if key in {"last_message_time", "next_trigger_time", "last_scheduled_at"}:
                if isinstance(merged[key], (int, float)) and isinstance(
                    incoming[key], (int, float)
                ):
                    merged[key] = max(merged[key], incoming[key])
                continue

            # 间隔三元组与 last_scheduled_at 绑定，采用调度时间较晚的一方。
            if key in {
                "last_schedule_min_interval_seconds",
                "last_schedule_max_interval_seconds",
                "last_schedule_random_interval_seconds",
            }:
                base_scheduled_at = merged.get("last_scheduled_at")
                incoming_scheduled_at = incoming.get("last_scheduled_at")
                if isinstance(base_scheduled_at, (int, float)) and isinstance(
                    incoming_scheduled_at, (int, float)
                ):
                    if incoming_scheduled_at >= base_scheduled_at:
                        merged[key] = incoming[key]
                elif key not in merged or not merged.get(key):
                    merged[key] = incoming[key]
                continue

            if merged[key] is None:
                merged[key] = incoming[key]
        return merged
