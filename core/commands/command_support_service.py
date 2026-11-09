"""指令层横切能力。

集中处理管理员校验、作用域参数解析（当前会话 / 全局 / 指定 UMO）、
计时器剩余时间读取与文本格式化，避免这些逻辑散落在各条指令里。
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger

from ..adapters.astrbot_platform import iter_active_platform_instances
from ..domain.session_key import is_group_type

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.platform.platform import PlatformStatus
except ImportError:  # pragma: no cover
    PlatformStatus = None  # type: ignore[assignment,misc]

# 作用域取值
SCOPE_CURRENT = "current"
SCOPE_GLOBAL = "global"
SCOPE_SESSION = "session"

# 作用域关键字别名（统一小写后匹配）
_SCOPE_ALIASES: dict[str, str] = {
    "当前会话": SCOPE_CURRENT,
    "当前": SCOPE_CURRENT,
    "本会话": SCOPE_CURRENT,
    "这个会话": SCOPE_CURRENT,
    "current": SCOPE_CURRENT,
    "here": SCOPE_CURRENT,
    "全局": SCOPE_GLOBAL,
    "全体": SCOPE_GLOBAL,
    "所有": SCOPE_GLOBAL,
    "all": SCOPE_GLOBAL,
    "global": SCOPE_GLOBAL,
}


class CommandSupportService:
    """指令支撑服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------
    async def is_plugin_admin(self, event: Any) -> bool:
        """宿主管理员直通，否则查插件管理员名单。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass

        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            return False

        if not sender_id:
            return False

        admins = self.plugin.config.get("admin_users") or []
        if not isinstance(admins, list):
            return False
        return sender_id in {str(item).strip() for item in admins if str(item).strip()}

    # ------------------------------------------------------------------
    # 会话与作用域
    # ------------------------------------------------------------------
    def current_session_id(self, event: Any) -> str:
        """取当前会话的规范 UMO。"""
        raw = getattr(event, "unified_msg_origin", "") or ""
        return self.plugin.session_service.normalize_session_id(str(raw))

    def resolve_scope(self, event: Any, token: str | None) -> tuple[str, str, str]:
        """解析作用域参数。

        Returns:
            (scope, target_umo, error)。error 非空表示参数非法。
        """
        current = self.current_session_id(event)
        raw = str(token or "").strip()
        if not raw:
            return SCOPE_CURRENT, current, ""

        lowered = raw.lower()
        if lowered in _SCOPE_ALIASES:
            scope = _SCOPE_ALIASES[lowered]
            if scope == SCOPE_GLOBAL:
                return SCOPE_GLOBAL, "", ""
            return SCOPE_CURRENT, current, ""

        # 其余一律按会话 UMO 处理：必须能解析成 platform:type:id。
        normalized = self.plugin.session_service.normalize_session_id(raw)
        if self.plugin.session_service.parse_session_id(normalized):
            return SCOPE_SESSION, normalized, ""

        return (
            "",
            "",
            f"无法识别作用域参数「{raw}」，可用：当前会话 / 全局 / 完整会话 UMO",
        )

    def resolve_single_session(self, event: Any, token: str | None) -> tuple[str, str]:
        """解析只接受单个会话的指令参数（发送 / 开关）。

        Returns:
            (session_id, error)。全局作用域需要调用方自行展开。
        """
        scope, target, error = self.resolve_scope(event, token)
        if error:
            return "", error
        if scope == SCOPE_GLOBAL:
            return "", ""
        return target, ""

    def is_group_session(self, session_id: str) -> bool:
        from ..domain.session_key import is_group_type

        parsed = self.plugin.session_service.parse_session_id(session_id)
        if not parsed:
            return "group" in str(session_id).lower()
        return is_group_type(parsed[1])

    def session_label(self, session_id: str, session_config: dict | None = None) -> str:
        """会话展示串：类型 + ID (备注名)。"""
        return self.plugin.session_service.get_session_log_str(
            session_id, session_config
        )

    # ------------------------------------------------------------------
    # 可启用会话枚举
    # ------------------------------------------------------------------
    # 配置分组 -> 裸 ID 应按哪种消息类型补全为完整 UMO。
    _SCOPE_MESSAGE_TYPES = {
        "friend_settings": "FriendMessage",
        "group_settings": "GroupMessage",
    }

    def _resolve_bare_sessions(self, raw: str, message_type: str) -> list[str]:
        """把配置里的裸 ID 补全为完整 UMO 列表；无法确定时返回空列表。

        优先复用已知会话的原始 UMO，从而保留 PrivateMessage / GuildMessage
        等具体类型，并返回该 target 在全部平台上的已知会话。
        没有已知会话时，只有在唯一运行平台下才能确定目标平台；
        多平台并存时宁可跳过并告警，也不要按平台顺序随便挑一个。
        """
        if not raw or not raw.strip():
            return []

        session_service = self.plugin.session_service
        known = session_service.find_known_umos_by_target(
            raw, group=is_group_type(message_type)
        )
        if known:
            resolved: list[str] = []
            for umo in known:
                normalized = session_service.normalize_session_id(umo)
                if normalized not in resolved:
                    resolved.append(normalized)
            return resolved

        running = self._running_platform_ids()
        if len(running) != 1:
            logger.warning(
                f"[主动消息] 配置中的裸会话 ID「{raw}」无法确定所属平台"
                f"（当前运行平台 {len(running)} 个），已跳过。"
                "请改用完整会话 UMO。"
            )
            return []
        return [f"{running[0]}:{message_type}:{raw}"]

    def _running_platform_ids(self) -> list[str]:
        """列出当前处于运行状态的平台实例 ID。"""
        try:
            instances = iter_active_platform_instances(
                self.plugin.context.platform_manager
            )
        except Exception as e:
            logger.debug(f"[主动消息] 读取平台实例失败喵: {e}")
            return []
        if PlatformStatus is None:
            return []
        return [
            str(platform.meta().id)
            for platform in instances
            if platform.status == PlatformStatus.RUNNING
        ]

    def list_enabled_sessions(self) -> list[str]:
        """枚举当前配置下真正生效（命中 session_list 且全局已启用）的会话。

        session_list 允许只写裸 target_id（ConfigService 就是这么匹配的），
        这里必须补全成完整 UMO 再返回：否则全局发送/开关拿裸 ID 去查配置
        会查不到，计时器与任务也无法按完整 UMO 匹配。
        """
        plugin = self.plugin
        result: list[str] = []
        for scope_key, message_type in self._SCOPE_MESSAGE_TYPES.items():
            settings = plugin.config.get(scope_key, {})
            if not settings.get("enable", False):
                continue
            for session in settings.get("session_list", []) or []:
                if not isinstance(session, str) or not session:
                    continue
                if plugin.session_service.parse_session_id(session):
                    # 完整 UMO 归一后只对应一个会话。
                    candidates = [plugin.session_service.normalize_session_id(session)]
                else:
                    # 裸 ID 可能命中同 target 在多个平台上的会话，全部展开。
                    candidates = self._resolve_bare_sessions(session, message_type)
                for normalized in candidates:
                    if normalized and normalized not in result:
                        result.append(normalized)
        return result

    def base_config_of(self, session_id: str) -> dict | None:
        """取会话的基础配置（不含 override）。"""
        return self.plugin.config_service.get_base_session_config(session_id)

    # ------------------------------------------------------------------
    # 计时器读取
    # ------------------------------------------------------------------
    def timer_remaining(self, timer: Any) -> int | None:
        """读取 asyncio 计时器剩余秒数；不可用时返回 None。"""
        if timer is None:
            return None
        try:
            if getattr(timer, "cancelled", lambda: False)():
                return None
        except Exception:
            return None

        when = getattr(timer, "when", None)
        if not callable(when):
            return None
        try:
            loop = getattr(timer, "_loop", None)
            if loop is None:
                return None
            return max(0, int(math.ceil(when() - loop.time())))
        except Exception:
            return None

    def pending_job_seconds(self, session_id: str) -> int | None:
        """读取 APScheduler 中该会话任务的剩余秒数。"""
        scheduler = getattr(self.plugin, "scheduler", None)
        if not scheduler:
            return None
        try:
            for job in scheduler.get_jobs():
                if str(job.id) != session_id:
                    continue
                next_run = getattr(job, "next_run_time", None)
                if next_run is None:
                    return None
                return max(0, int(next_run.timestamp() - time.time()))
        except Exception:
            return None
        return None

    def describe_next_trigger(
        self, session_id: str, session_config: dict
    ) -> dict[str, Any]:
        """汇总该会话“下一次主动消息”的时间信息。

        Returns:
            {"kind": str, "remaining": int | None, "target_time": float | None}
            kind 取值：group_silence / auto_trigger / schedule / none
        """
        plugin = self.plugin

        # 群聊优先看沉默计时器：它决定本轮的话题窗口。
        if self.is_group_session(session_id):
            remaining = self.timer_remaining(plugin.group_timers.get(session_id))
            if remaining is not None:
                return {
                    "kind": "group_silence",
                    "remaining": remaining,
                    "target_time": time.time() + remaining,
                }

        remaining = self.timer_remaining(plugin.auto_trigger_timers.get(session_id))
        if remaining is not None:
            return {
                "kind": "auto_trigger",
                "remaining": remaining,
                "target_time": time.time() + remaining,
            }

        remaining = self.pending_job_seconds(session_id)
        if remaining is not None:
            return {
                "kind": "schedule",
                "remaining": remaining,
                "target_time": time.time() + remaining,
            }

        return {"kind": "none", "remaining": None, "target_time": None}

    # ------------------------------------------------------------------
    # 格式化
    # ------------------------------------------------------------------
    def format_duration(self, seconds: int | None) -> str:
        """把秒数格式化成「X 天 X 小时 X 分 X 秒」。"""
        if seconds is None:
            return "未知"
        total = max(0, int(seconds))
        if total == 0:
            return "0 秒"

        days, rest = divmod(total, 86400)
        hours, rest = divmod(rest, 3600)
        minutes, secs = divmod(rest, 60)

        parts: list[str] = []
        if days:
            parts.append(f"{days} 天")
        if hours:
            parts.append(f"{hours} 小时")
        if minutes:
            parts.append(f"{minutes} 分")
        if secs or not parts:
            parts.append(f"{secs} 秒")
        return " ".join(parts)

    def format_clock(self, timestamp: float) -> str:
        """把时间戳按插件时区格式化为 YYYY-MM-DD HH:MM:SS。"""
        tz = getattr(self.plugin, "timezone", None)
        try:
            dt = (
                datetime.fromtimestamp(timestamp, tz=tz)
                if tz
                else datetime.fromtimestamp(timestamp)
            )
        except Exception:
            return "未知"
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def parse_switch(token: str | None) -> bool | None:
        """解析开关参数；返回 None 表示“未提供，切换当前状态”。

        注意：无法识别的词同样会得到 None，调用方必须先判断
        参数是否为空，否则拼错一个字母就会静默反转开关状态。
        """
        raw = str(token or "").strip().lower()
        if not raw:
            return None
        if raw in {"on", "开", "开启", "启用", "true", "1", "yes"}:
            return True
        if raw in {"off", "关", "关闭", "禁用", "false", "0", "no"}:
            return False
        return None

    @classmethod
    def is_switch_token(cls, token: str | None) -> bool:
        """判断参数是否为可识别的开关词（未提供与拼错都返回 False）。"""
        return cls.parse_switch(token) is not None

    @staticmethod
    def format_quiet_hours(schedule_settings: dict) -> str:
        """描述免打扰配置的当前状态。"""
        if not schedule_settings.get("enable_quiet_hours", True):
            return "未启用"
        text = str(schedule_settings.get("quiet_hours", "") or "").strip()
        return text if text else "已启用但未设置时段"
