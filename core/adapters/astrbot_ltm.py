"""AstrBot 平台消息流水（LTM）适配器。

本模块集中收敛与核心 provider_ltm_settings / message_history_manager 的交互：
- 群聊流水开关与保留条数的读取（读取侧与补写侧共用同一份判定）
- insert() 的版本差异（max_messages 参数在旧版本中不存在）
"""

from __future__ import annotations

import inspect
from typing import Any

# 核心 group_message_history_max_cnt 的默认值，与 default.py 保持一致。
DEFAULT_GROUP_HISTORY_MAX = 700


def read_group_history_settings(context: Any, umo: str) -> tuple[bool, int]:
    """读取核心群聊流水开关与保留条数，返回 (是否启用, 条数上限)。

    核心的写入（群友消息与 Bot 回复）受 provider_ltm_settings 控制，关闭时
    流水表里不会有真实群友消息。读取与补写前都要感知这一点，否则用户会在
    默认配置下误以为插件失效，或写入核心查询侧不认的孤儿数据。

    读取失败时按“未启用 + 默认条数”处理，让调用方在异常时保守跳过写入。
    """
    config = None
    # 优先按会话读取；旧版本 get_config 不支持 umo 参数时退回全局配置。
    for kwargs in ({"umo": umo}, {}):
        try:
            config = context.get_config(**kwargs) or {}
            break
        except TypeError:
            continue
        except Exception:
            config = None
            break

    if not hasattr(config, "get"):
        return False, DEFAULT_GROUP_HISTORY_MAX

    settings = config.get("provider_ltm_settings", {})
    if not isinstance(settings, dict):
        return False, DEFAULT_GROUP_HISTORY_MAX

    # 该配置项在 core/config/default.py 中声明为 bool，无需额外的字符串解析。
    enabled = bool(settings.get("group_message_history_enable", False))
    try:
        max_messages = max(
            1,
            int(
                settings.get("group_message_history_max_cnt", DEFAULT_GROUP_HISTORY_MAX)
            ),
        )
    except (TypeError, ValueError):
        max_messages = DEFAULT_GROUP_HISTORY_MAX
    return enabled, max_messages


def supports_max_messages(history_mgr: Any) -> bool:
    """探测核心 insert() 是否接受 max_messages 参数。

    AstrBot 4.24.x 的 insert() 尚无该参数，而插件声明支持 >=4.24.0，
    因此必须探测后再传，否则补写会抛 TypeError 导致记录丢失。
    """
    insert = getattr(history_mgr, "insert", None)
    if not callable(insert):
        return False
    try:
        return "max_messages" in inspect.signature(insert).parameters
    except (TypeError, ValueError):  # pragma: no cover - 不可内省的实现
        return False


__all__ = [
    "DEFAULT_GROUP_HISTORY_MAX",
    "read_group_history_settings",
    "supports_max_messages",
]
