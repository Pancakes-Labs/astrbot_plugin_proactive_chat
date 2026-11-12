"""配置结构与键名的唯一事实来源。

集中定义后，schema、迁移器、运行时读取与前端契约都从这里对齐，
避免键名散落在各服务里各写一份字符串。
"""

from __future__ import annotations

# ----------------------------------------------------------------------
# 顶层分组
# ----------------------------------------------------------------------
# 合并后唯一的会话默认配置分组（对应 session_overrides 的对偶）
SESSION_DEFAULTS_KEY = "session_defaults"
# 迁移期的隐藏类型回退层：承载旧结构里私聊/群聊的差异值
LEGACY_TYPE_FALLBACK_KEY = "legacy_type_fallback"
ADMIN_USERS_KEY = "admin_users"
WEB_ADMIN_KEY = "web_admin"
NOTIFICATION_SETTINGS_KEY = "notification_settings"
TELEMETRY_CONFIG_KEY = "telemetry_config"

# 旧分组：仅用于迁移读取，迁移完成后置空
LEGACY_FRIEND_SETTINGS_KEY = "friend_settings"
LEGACY_GROUP_SETTINGS_KEY = "group_settings"
LEGACY_SESSION_SETTINGS_KEYS: tuple[str, ...] = (
    LEGACY_FRIEND_SETTINGS_KEY,
    LEGACY_GROUP_SETTINGS_KEY,
)

# ----------------------------------------------------------------------
# session_defaults 内的二级分组
# ----------------------------------------------------------------------
ENABLED_KEY = "enabled"
SESSION_LIST_KEY = "session_list"
PERSONA_ID_KEY = "persona_id"
PROMPT_SETTINGS_KEY = "prompt_settings"
# prompt_settings 内部的输出格式设置：预设值 + 自定义正文
OUTPUT_FORMAT_KEY = "output_format"
OUTPUT_FORMAT_CUSTOM_KEY = "output_format_custom"
CONTEXT_SETTINGS_KEY = "context_settings"
SCHEDULE_SETTINGS_KEY = "schedule_settings"
STARTUP_CHECK_SETTINGS_KEY = "startup_check_settings"
GROUP_SILENCE_SETTINGS_KEY = "group_silence_settings"
TTS_SETTINGS_KEY = "tts_settings"
SEGMENTED_REPLY_SETTINGS_KEY = "segmented_reply_settings"

# 仅对群聊注入的二级分组：运行时按会话类型选择性合入
GROUP_ONLY_SECTIONS: tuple[str, ...] = (GROUP_SILENCE_SETTINGS_KEY,)

# 会话覆写白名单：与 session_defaults 的二级结构保持同形
OVERRIDABLE_ROOT_KEYS: frozenset[str] = frozenset(
    {
        "session_name",
        ENABLED_KEY,
        PERSONA_ID_KEY,
        PROMPT_SETTINGS_KEY,
        CONTEXT_SETTINGS_KEY,
        SCHEDULE_SETTINGS_KEY,
        STARTUP_CHECK_SETTINGS_KEY,
        GROUP_SILENCE_SETTINGS_KEY,
        TTS_SETTINGS_KEY,
        SEGMENTED_REPLY_SETTINGS_KEY,
    }
)

# ----------------------------------------------------------------------
# 旧键 -> 新键映射（迁移与会话覆写共用）
# ----------------------------------------------------------------------
# 展平后的点号路径映射；只描述“换了名字且换了层级”的字段。
LEGACY_KEY_MAP: dict[str, str] = {
    f"{SCHEDULE_SETTINGS_KEY}.min_interval_minutes": (
        f"{SCHEDULE_SETTINGS_KEY}.min_delay_minutes"
    ),
    f"{SCHEDULE_SETTINGS_KEY}.max_interval_minutes": (
        f"{SCHEDULE_SETTINGS_KEY}.max_delay_minutes"
    ),
    f"{SCHEDULE_SETTINGS_KEY}.enable_quiet_hours": (
        f"{SCHEDULE_SETTINGS_KEY}.quiet_hours_enabled"
    ),
    "auto_trigger_settings.enable_auto_trigger": (
        f"{STARTUP_CHECK_SETTINGS_KEY}.enabled"
    ),
    "auto_trigger_settings.auto_trigger_after_minutes": (
        f"{STARTUP_CHECK_SETTINGS_KEY}.after_silence_minutes"
    ),
    f"{TTS_SETTINGS_KEY}.enable_tts": f"{TTS_SETTINGS_KEY}.enabled",
    f"{SEGMENTED_REPLY_SETTINGS_KEY}.enable": (
        f"{SEGMENTED_REPLY_SETTINGS_KEY}.enabled"
    ),
    f"{SEGMENTED_REPLY_SETTINGS_KEY}.enable_content_cleanup": (
        f"{SEGMENTED_REPLY_SETTINGS_KEY}.content_cleanup_enabled"
    ),
}

# 整组搬迁的旧分组 -> 新分组前缀
LEGACY_SECTION_MAP: dict[str, str] = {
    "auto_trigger_settings": STARTUP_CHECK_SETTINGS_KEY,
}

# 旧版顶层标量 -> 新位置
LEGACY_ROOT_MAP: dict[str, str] = {
    "enable": ENABLED_KEY,
    "group_idle_trigger_minutes": f"{GROUP_SILENCE_SETTINGS_KEY}.idle_minutes",
}

__all__ = [
    "ADMIN_USERS_KEY",
    "CONTEXT_SETTINGS_KEY",
    "ENABLED_KEY",
    "GROUP_ONLY_SECTIONS",
    "GROUP_SILENCE_SETTINGS_KEY",
    "LEGACY_FRIEND_SETTINGS_KEY",
    "LEGACY_GROUP_SETTINGS_KEY",
    "LEGACY_KEY_MAP",
    "LEGACY_ROOT_MAP",
    "LEGACY_SECTION_MAP",
    "LEGACY_SESSION_SETTINGS_KEYS",
    "LEGACY_TYPE_FALLBACK_KEY",
    "NOTIFICATION_SETTINGS_KEY",
    "OUTPUT_FORMAT_CUSTOM_KEY",
    "OUTPUT_FORMAT_KEY",
    "OVERRIDABLE_ROOT_KEYS",
    "PERSONA_ID_KEY",
    "PROMPT_SETTINGS_KEY",
    "SCHEDULE_SETTINGS_KEY",
    "SEGMENTED_REPLY_SETTINGS_KEY",
    "SESSION_DEFAULTS_KEY",
    "SESSION_LIST_KEY",
    "STARTUP_CHECK_SETTINGS_KEY",
    "TELEMETRY_CONFIG_KEY",
    "TTS_SETTINGS_KEY",
    "WEB_ADMIN_KEY",
]
