"""插件指令层。

指令的业务实现与横切能力都收敛在本包内，main.py 只保留
@filter.command 薄壳，负责把事件转交给 PluginCommandService。
"""

from __future__ import annotations

from .command_support_service import CommandSupportService
from .plugin_command_service import PluginCommandService

__all__ = [
    "CommandSupportService",
    "PluginCommandService",
]
