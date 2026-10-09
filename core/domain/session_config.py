"""会话配置的强类型视图。

本模块是既有 dict 配置的只读包装，不改变任何底层结构，
只提供语义化访问器，供 features 使用，避免在业务代码里散落 .get 调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 一级配置块名称（与 _conf_schema.json 完全一致）
FRIEND_SETTINGS_KEY = "friend_settings"
GROUP_SETTINGS_KEY = "group_settings"
WEB_ADMIN_KEY = "web_admin"
NOTIFICATION_SETTINGS_KEY = "notification_settings"
TELEMETRY_CONFIG_KEY = "telemetry_config"

# 会话类型标签
SESSION_TYPE_FRIEND = "friend"
SESSION_TYPE_GROUP = "group"


@dataclass
class SessionConfig:
    """会话最终生效配置的只读视图。

    Attributes:
        raw: 合并 base + override 后的原始 dict，保持与旧实现完全一致的结构。
        session_type: friend 或 group。
        from_session_list: 是否命中全局 session_list。
        has_override: 是否存在会话级覆写。
    """

    raw: dict[str, Any] = field(default_factory=dict)
    session_type: str = ""
    from_session_list: bool = False
    has_override: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SessionConfig | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            raw=payload,
            session_type=str(payload.get("_session_type") or ""),
            from_session_list=bool(payload.get("_from_session_list", False)),
            has_override=bool(payload.get("_has_override", False)),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enable", False))

    def section(self, key: str) -> dict[str, Any]:
        """读取某个配置段，非 dict 时返回空 dict。"""
        value = self.raw.get(key)
        return value if isinstance(value, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)
