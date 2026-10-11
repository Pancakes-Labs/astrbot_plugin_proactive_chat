"""领域层：纯数据模型与纯函数，不依赖任何框架。

本层是全项目的稳定契约，任何 services / features / adapters 都只能依赖它，
而它自身不反向依赖任何上层模块，也不导入 astrbot。
"""

from .domain_enums import HookPoint, StageResult, TriggerSource
from .session_key import (
    SessionKey,
    contains_group_marker,
    is_friend_type,
    is_group_type,
    is_group_umo,
)

__all__ = [
    "HookPoint",
    "StageResult",
    "TriggerSource",
    "SessionKey",
    "contains_group_marker",
    "is_friend_type",
    "is_group_type",
    "is_group_umo",
]
