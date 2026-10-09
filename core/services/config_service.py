"""配置读取与验证服务。

由旧 core/session_config.py 等价搬迁而来。
"""

from __future__ import annotations

import copy
from typing import Any

from astrbot.api import logger

from ..domain.session_key import is_friend_type, is_group_type


class ConfigService:
    """配置校验与会话配置路由服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    async def validate_config(self) -> None:
        """验证插件配置的完整性和有效性。"""
        config = self.plugin.config
        try:
            friend_settings = config.get("friend_settings", {})
            group_settings = config.get("group_settings", {})

            if friend_settings.get("enable", False):
                session_list = friend_settings.get("session_list", [])
                if not session_list:
                    logger.warning(
                        "[主动消息] 私聊主动消息已启用但未配置任何会话喵（session_list 为空）。"
                    )

                schedule_settings = friend_settings.get("schedule_settings", {})
                min_interval = schedule_settings.get("min_interval_minutes", 30)
                max_interval = schedule_settings.get("max_interval_minutes", 900)
                if min_interval > max_interval:
                    logger.warning(
                        "[主动消息] 私聊主动消息配置中最小间隔大于最大间隔喵，将自动调整喵。"
                    )

            if group_settings.get("enable", False):
                session_list = group_settings.get("session_list", [])
                if not session_list:
                    logger.warning(
                        "[主动消息] 群聊主动消息已启用但未配置任何会话喵（session_list 为空）。"
                    )

            logger.info("[主动消息] 配置验证完成喵。")

        except Exception as e:
            logger.error(f"[主动消息] 配置验证过程出错喵: {e}")
            raise

    def get_session_config(self, session_id: str) -> dict | None:
        """根据会话 UMO 获取最终生效配置（base + override）。"""
        base = self.get_base_session_config(session_id)
        if not base:
            return None
        return self.build_effective_config(session_id, base)

    def get_base_session_config(self, session_id: str) -> dict | None:
        """获取仅由全局配置与会话命中规则决定的基础配置。"""
        parsed = self.plugin.session_service.parse_session_id(session_id)
        if not parsed:
            return None

        _, message_type, target_id = parsed
        if is_friend_type(message_type):
            return self._get_typed_session_config(
                session_id, target_id, "friend_settings", "friend"
            )
        if is_group_type(message_type):
            return self._get_typed_session_config(
                session_id, target_id, "group_settings", "group"
            )
        return None

    def build_effective_config(
        self, session_id: str, base_config: dict | None
    ) -> dict | None:
        """将会话差异补丁合并到基础配置，返回最终生效配置。"""
        if not base_config:
            return None

        manager = getattr(self.plugin, "session_override_manager", None)
        if not manager:
            return base_config

        normalized_session_id = self.plugin.session_service.normalize_session_id(
            session_id
        )
        effective = manager.get_effective(normalized_session_id, base_config)

        if isinstance(effective, dict):
            effective["_session_type"] = base_config.get("_session_type")
            effective["_from_session_list"] = base_config.get(
                "_from_session_list", False
            )
            effective["_has_override"] = bool(
                manager.get_override(normalized_session_id)
            )

        return effective

    def _get_typed_session_config(
        self, session_id: str, target_id: str, settings_key: str, session_type: str
    ) -> dict | None:
        settings = self.plugin.config.get(settings_key, {})
        if not settings.get("enable", False):
            return None

        session_list = settings.get("session_list", [])
        normalized_session_id = self.plugin.session_service.normalize_session_id(
            session_id
        )
        candidates = {session_id, normalized_session_id, target_id}

        if any(candidate in session_list for candidate in candidates):
            config_copy = copy.deepcopy(settings)
            config_copy["_session_type"] = session_type
            config_copy["_from_session_list"] = True
            return config_copy

        return None

    def get_friend_session_config(self, session_id: str, target_id: str) -> dict | None:
        return self._get_typed_session_config(
            session_id, target_id, "friend_settings", "friend"
        )

    def get_group_session_config(self, session_id: str, target_id: str) -> dict | None:
        return self._get_typed_session_config(
            session_id, target_id, "group_settings", "group"
        )
