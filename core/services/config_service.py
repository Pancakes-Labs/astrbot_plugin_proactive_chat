"""配置读取与验证服务。

会话配置解析链（由外到内，后者覆盖前者）：
1. session_defaults：全局默认值；
2. legacy_type_fallback：迁移期保留的“私聊/群聊确有差异”的值，按会话类型合入；
3. 群聊专属分组：仅群聊注入，私聊忽略；
4. session_overrides：用户为该会话显式设置的差异。
"""

from __future__ import annotations

import copy
from typing import Any

from astrbot.api import logger

from ..domain import config_keys as ck
from ..domain.model_scene import format_model_ref, parse_model_ref_list
from ..domain.session_key import is_friend_type, is_group_type

# 会话类型标签（与 PromptBlock / 前端约定一致）
SESSION_TYPE_FRIEND = "friend"
SESSION_TYPE_GROUP = "group"


class ConfigService:
    """配置校验与会话配置路由服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    async def validate_config(self) -> None:
        """验证插件配置的完整性和有效性。"""
        config = self.plugin.config
        try:
            defaults = config.get(ck.SESSION_DEFAULTS_KEY, {})
            if not isinstance(defaults, dict):
                defaults = {}

            if defaults.get(ck.ENABLED_KEY, False):
                session_list = defaults.get(ck.SESSION_LIST_KEY, [])
                if not session_list:
                    logger.warning(
                        "[主动消息] 主动消息已启用但未配置任何会话喵（session_list 为空）。"
                    )
                else:
                    # 裸 ID 自 v1.2 起不再支持，启动阶段一次性提示，
                    # 避免用户在毫不知情的情况下发现某个会话"凭空消失"。
                    bare_ids = [
                        item
                        for item in session_list
                        if isinstance(item, str)
                        and item
                        and self.plugin.session_service.parse_session_id(item) is None
                    ]
                    if bare_ids:
                        logger.warning(
                            "[主动消息] 会话列表中存在裸会话 ID，已不再支持并会被跳过喵: "
                            f"{', '.join(bare_ids)}。"
                            "请改用完整会话 UMO（例如 default:FriendMessage:123456789）。"
                        )

                schedule = defaults.get(ck.SCHEDULE_SETTINGS_KEY, {})
                if isinstance(schedule, dict):
                    min_delay = schedule.get("min_delay_minutes", 30)
                    max_delay = schedule.get("max_delay_minutes", 600)
                    if min_delay > max_delay:
                        logger.warning(
                            "[主动消息] 最短发送延迟大于最长发送延迟喵，将按最短延迟处理喵。"
                        )

                group_silence = defaults.get(ck.GROUP_SILENCE_SETTINGS_KEY, {})
                if isinstance(group_silence, dict):
                    idle = int(group_silence.get("idle_minutes", 0) or 0)
                    if idle <= 0:
                        logger.warning(
                            "[主动消息] 群聊沉默判定时长为 0，群聊主动消息不可用喵。"
                        )

            self._validate_model_settings(defaults)

            logger.info("[主动消息] 配置验证完成喵。")

        except Exception as e:
            logger.error(f"[主动消息] 配置验证过程出错喵: {e}")
            raise

    def _validate_model_settings(self, defaults: dict) -> None:
        """校验模型场景配置，主要拦截"填了但不存在"的回退候选。

        这一步只在启动期做一次：用户填错 Provider ID 时立刻能看到提示，
        而不是等到真正故障触发回退、恰好经过那个坏候选时才暴露。
        """
        model_settings = defaults.get(ck.MODEL_SETTINGS_KEY)
        if not isinstance(model_settings, dict):
            return

        known_ids = self._known_provider_ids()
        scenes = model_settings.get("scenes")
        if not isinstance(scenes, dict):
            return

        for scene_name, node in scenes.items():
            if not isinstance(node, dict):
                continue
            invalid: list[str] = []

            provider_id = str(node.get("provider_id") or "").strip()
            if provider_id and known_ids is not None and provider_id not in known_ids:
                invalid.append(f"provider_id={provider_id}")

            refs, malformed = parse_model_ref_list(node.get("fallback_chain"))
            invalid.extend(malformed)
            if known_ids is not None:
                for ref in refs:
                    if ref.provider_id and ref.provider_id not in known_ids:
                        invalid.append(format_model_ref(ref))

            if invalid:
                logger.warning(
                    f"[主动消息] 模型场景 {scene_name} 存在不可用的候选喵: "
                    f"{', '.join(invalid)}。这些候选在实际调用时会被跳过，"
                    "请检查服务提供商是否已被删除或改名。"
                )

    def _known_provider_ids(self) -> set[str] | None:
        """读取当前已注册的 Provider ID 集合；无法读取时返回 None。"""
        manager = getattr(
            getattr(self.plugin, "context", None), "provider_manager", None
        )
        if manager is None:
            return None
        try:
            insts = manager.inst_map
        except Exception:
            return None
        if not isinstance(insts, dict):
            return None
        return {str(key) for key in insts.keys()}

    # ------------------------------------------------------------------
    # 会话配置解析
    # ------------------------------------------------------------------
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

        _, message_type, _ = parsed
        if is_friend_type(message_type):
            session_type = SESSION_TYPE_FRIEND
        elif is_group_type(message_type):
            session_type = SESSION_TYPE_GROUP
        else:
            return None

        return self._get_typed_session_config(session_id, session_type)

    def _get_typed_session_config(
        self,
        session_id: str,
        session_type: str,
    ) -> dict | None:
        """按会话类型组装基础配置。"""
        config = self.plugin.config
        defaults = config.get(ck.SESSION_DEFAULTS_KEY, {})
        if not isinstance(defaults, dict):
            return None

        if not defaults.get(ck.ENABLED_KEY, False):
            return None

        session_list = defaults.get(ck.SESSION_LIST_KEY, [])
        if not isinstance(session_list, list):
            session_list = []

        # 会话 UMO 已由核心与配置统一为标准形式，直接精确比对即可。
        if session_id not in session_list:
            return None

        config_copy = copy.deepcopy(defaults)

        # 类型回退层：只在存在迁移差异时合入，优先级低于会话覆写。
        # 就地写入 config_copy，无需使用返回值。
        self._apply_type_fallback(config_copy, session_type, config)

        # 群聊专属分组：私聊必须剔除，否则私聊会读到群聊沉默时长等无关配置。
        if session_type != SESSION_TYPE_GROUP:
            for key in ck.GROUP_ONLY_SECTIONS:
                config_copy.pop(key, None)

        context_settings = config_copy.get(ck.CONTEXT_SETTINGS_KEY)
        if isinstance(context_settings, dict):
            mode = str(context_settings.get("source_mode") or "").strip()
            if not mode or mode == "auto":
                # auto：私聊用对话历史、群聊用平台流水，等价旧版两套默认值。
                context_settings["source_mode"] = (
                    "platform_message_history"
                    if session_type == SESSION_TYPE_GROUP
                    else "conversation_history"
                )

        config_copy["_session_type"] = session_type
        config_copy["_from_session_list"] = True

        return config_copy

    def _apply_type_fallback(
        self,
        config_copy: dict[str, Any],
        session_type: str,
        config: dict[str, Any],
    ) -> bool:
        """把迁移期保留的类型差异按会话类型合入配置，返回是否合入过。"""
        fallback = config.get(ck.LEGACY_TYPE_FALLBACK_KEY)
        if not isinstance(fallback, dict):
            return False

        section = fallback.get(session_type)
        if not isinstance(section, dict) or not section:
            return False

        def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    _merge(target[key], value)
                    continue
                target[key] = copy.deepcopy(value)

        _merge(config_copy, section)
        return True

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
            override = manager.get_override(normalized_session_id)
            effective["_session_type"] = base_config.get("_session_type")
            effective["_from_session_list"] = base_config.get(
                "_from_session_list", False
            )
            effective["_has_override"] = bool(override)

        return effective


__all__ = ["ConfigService", "SESSION_TYPE_FRIEND", "SESSION_TYPE_GROUP"]
