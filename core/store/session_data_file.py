"""会话数据文件的原子读写与损坏回滚。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import aiofiles
import aiofiles.os as aio_os

# 备份文件后缀：与主文件同目录，命名可预测便于人工排查。
BACKUP_SUFFIX = ".bak"
TEMP_SUFFIX = ".tmp"

# 读取结果的来源标记。
SOURCE_PRIMARY = "primary"
SOURCE_BACKUP = "backup"
SOURCE_EMPTY = "empty"


def backup_path_for(path: Path) -> Path:
    """返回主文件对应的备份路径。"""
    return path.with_name(path.name + BACKUP_SUFFIX)


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=4, ensure_ascii=False)


async def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    keep_backup: bool = True,
) -> None:
    """原子写入 JSON。

    先完整写入临时文件，再用 os.replace 整体替换目标文件。
    os.replace 在同一分区上是原子操作，因此任何时刻都不会出现半截文件。
    keep_backup=True 时会把旧文件改名为 .bak 作为回滚点；
    从备份恢复写回时应传 False，否则会把已经损坏的主文件盖到好备份上。
    """
    path = Path(path)
    await aio_os.makedirs(path.parent, exist_ok=True)

    content = await asyncio.to_thread(_dump, payload)
    temp_path = path.with_name(path.name + TEMP_SUFFIX)

    async with aiofiles.open(temp_path, "w", encoding="utf-8") as f:
        await f.write(content)
        await f.flush()

    if keep_backup:
        # 备份失败不应阻断主流程：哪怕没有回滚点也要保证数据能存下去。
        try:
            if await aio_os.path.exists(path):
                os.replace(path, backup_path_for(path))
        except OSError:
            pass

    await asyncio.to_thread(os.replace, temp_path, path)


async def _try_read_json(path: Path) -> tuple[dict | None, str]:
    """尝试读取一个 JSON 对象文件。

    Returns:
        (数据, 错误描述)。数据为 None 表示不可用；
        错误描述为空串表示「文件不存在」，非空表示「文件存在但不可用」。
    """
    if not await aio_os.path.exists(path):
        return None, ""

    try:
        async with aiofiles.open(path, encoding="utf-8") as f:
            content = await f.read()
        loaded = await asyncio.to_thread(json.loads, content)
    except (OSError, json.JSONDecodeError) as e:
        return None, f"{type(e).__name__}: {e}"

    if not isinstance(loaded, dict):
        return None, "根节点不是 JSON 对象"
    return loaded, ""


async def read_json_with_recovery(
    path: Path,
) -> tuple[dict, str, str]:
    """读取 JSON，主文件损坏时自动回退到 .bak。

    Returns:
        (数据, 来源标记, 说明)。
        来源标记为 primary / backup / empty；
        empty 时说明既没有可用主文件也没有可用备份，数据只能是空字典。
    """
    path = Path(path)

    primary, primary_error = await _try_read_json(path)
    if primary is not None:
        return primary, SOURCE_PRIMARY, ""

    backup, backup_error = await _try_read_json(backup_path_for(path))
    if backup is not None:
        return backup, SOURCE_BACKUP, primary_error

    if primary_error:
        return {}, SOURCE_EMPTY, primary_error
    if backup_error:
        return {}, SOURCE_EMPTY, backup_error
    # 主备都不存在：全新安装，属于正常情况。
    return {}, SOURCE_EMPTY, ""
