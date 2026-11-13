"""会话配置的强类型视图。

本模块是既有 dict 配置的只读包装，不改变任何底层结构，
只提供语义化访问器，供 features 使用，避免在业务代码里散落 .get 调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config_keys import (
    ADMIN_USERS_KEY,
    CONTEXT_SETTINGS_KEY,
    ENABLED_KEY,
    GROUP_SILENCE_SETTINGS_KEY,
    LEGACY_FRIEND_SETTINGS_KEY,
    LEGACY_GROUP_SETTINGS_KEY,
    LEGACY_TYPE_FALLBACK_KEY,
    MODEL_SETTINGS_KEY,
    NOTIFICATION_SETTINGS_KEY,
    PERSONA_ID_KEY,
    PROMPT_SETTINGS_KEY,
    SCHEDULE_SETTINGS_KEY,
    SEGMENTED_REPLY_SETTINGS_KEY,
    SESSION_DEFAULTS_KEY,
    SESSION_LIST_KEY,
    STARTUP_CHECK_SETTINGS_KEY,
    TELEMETRY_CONFIG_KEY,
    TTS_SETTINGS_KEY,
    WEB_ADMIN_KEY,
)

# 会话类型标签
SESSION_TYPE_FRIEND = "friend"
SESSION_TYPE_GROUP = "group"


@dataclass
class SessionConfig:
    """会话最终生效配置的只读视图。

    Attributes:
        raw: 合并 base + override 后的原始 dict，保持与运行时结构一致。
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
        return bool(self.raw.get(ENABLED_KEY, False))

    def section(self, key: str) -> dict[str, Any]:
        """读取某个配置段，非 dict 时返回空 dict。"""
        value = self.raw.get(key)
        return value if isinstance(value, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def persona_id(self) -> str:
        return str(self.raw.get(PERSONA_ID_KEY) or "").strip()

    @property
    def schedule_settings(self) -> dict[str, Any]:
        return self.section(SCHEDULE_SETTINGS_KEY)

    @property
    def context_settings(self) -> dict[str, Any]:
        return self.section(CONTEXT_SETTINGS_KEY)

    @property
    def prompt_settings(self) -> dict[str, Any]:
        return self.section(PROMPT_SETTINGS_KEY)

    @property
    def model_settings(self) -> dict[str, Any]:
        """模型场景配置（Provider / 模型 / 回退链）。"""
        return self.section(MODEL_SETTINGS_KEY)

    def scene_model_settings(self, scene: str) -> dict[str, Any]:
        """读取单个场景的模型配置；缺失时返回空 dict。

        scene 由调用方传入规范名（见 ModelScene），这里不做别名归一：
        归一属于领域层的职责，重复实现会导致两处规则漂移。
        """
        scenes = self.model_settings.get("scenes")
        if not isinstance(scenes, dict):
            return {}
        node = scenes.get(scene)
        return node if isinstance(node, dict) else {}

    @property
    def failure_policy(self) -> dict[str, Any]:
        """模型调用失败处置策略。"""
        policy = self.model_settings.get("failure_policy")
        return policy if isinstance(policy, dict) else {}

    @property
    def startup_check_settings(self) -> dict[str, Any]:
        return self.section(STARTUP_CHECK_SETTINGS_KEY)

    @property
    def group_silence_settings(self) -> dict[str, Any]:
        return self.section(GROUP_SILENCE_SETTINGS_KEY)

    @property
    def tts_settings(self) -> dict[str, Any]:
        return self.section(TTS_SETTINGS_KEY)

    @property
    def segmented_reply_settings(self) -> dict[str, Any]:
        return self.section(SEGMENTED_REPLY_SETTINGS_KEY)

    @property
    def quiet_hours_enabled(self) -> bool:
        """是否启用免打扰时段；未显式配置时按启用处理。"""
        return bool(self.schedule_settings.get("quiet_hours_enabled", True))


__all__ = [
    "ADMIN_USERS_KEY",
    "CONTEXT_SETTINGS_KEY",
    "ENABLED_KEY",
    "GROUP_SILENCE_SETTINGS_KEY",
    "LEGACY_FRIEND_SETTINGS_KEY",
    "LEGACY_GROUP_SETTINGS_KEY",
    "LEGACY_TYPE_FALLBACK_KEY",
    "MODEL_SETTINGS_KEY",
    "NOTIFICATION_SETTINGS_KEY",
    "PERSONA_ID_KEY",
    "PROMPT_SETTINGS_KEY",
    "SCHEDULE_SETTINGS_KEY",
    "SEGMENTED_REPLY_SETTINGS_KEY",
    "SESSION_DEFAULTS_KEY",
    "SESSION_LIST_KEY",
    "SESSION_TYPE_FRIEND",
    "SESSION_TYPE_GROUP",
    "STARTUP_CHECK_SETTINGS_KEY",
    "SessionConfig",
    "TELEMETRY_CONFIG_KEY",
    "TTS_SETTINGS_KEY",
    "WEB_ADMIN_KEY",
]
