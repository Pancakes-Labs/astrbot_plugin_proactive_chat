"""框架适配层：隔离 AstrBot 框架细节。

本层负责与 AstrBot 的事件、平台、对话、Provider 等 API 交互，
向上层 services / features 暴露稳定接口，避免框架细节侵入领域逻辑。
"""

from .astrbot_event import (
    ProactiveMessageEvent,
    build_proactive_event_for_session,
    dispatch_event_hook,
)
from .astrbot_platform import (
    find_platform_by_id,
    iter_active_platform_instances,
    resolve_platform_instance,
)

__all__ = [
    "ProactiveMessageEvent",
    "build_proactive_event_for_session",
    "dispatch_event_hook",
    "find_platform_by_id",
    "iter_active_platform_instances",
    "resolve_platform_instance",
]
