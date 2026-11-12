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
from ..domain.config_keys import (
    ENABLED_KEY,
    SESSION_DEFAULTS_KEY,
    SESSION_LIST_KEY,
)
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
    # 裸 ID 补全时按消息类型分别处理：会话列表已合并，只能按解析结果分流。
    _FRIEND_MESSAGE_TYPE = "FriendMessage"
    _GROUP_MESSAGE_TYPE = "GroupMessage"

    def _lookup_known_sessions(
        self, raw: str, message_type: str
    ) -> tuple[list[str], bool]:
        """只查已知会话，不做任何平台猜测补全。

        Returns:
            (可用 UMO 列表, 是否存在已离线但已知的目标会话)。
            第二个返回值必须向上传递：只要该目标在某个平台上有已知会话，
            就不能退化成「按唯一运行平台猜一个 UMO」，否则会把消息
            发到别的平台上的同 ID 目标。
        """
        if not raw or not raw.strip():
            return [], False

        session_service = self.plugin.session_service
        known, has_unavailable = session_service.find_known_umos_by_target(
            raw, group=is_group_type(message_type)
        )
        resolved: list[str] = []
        for umo in known or []:
            normalized = session_service.normalize_session_id(umo)
            if normalized not in resolved:
                resolved.append(normalized)

        if not resolved and has_unavailable:
            logger.warning(
                f"[主动消息] 配置中的裸会话 ID「{raw}」对应的已知会话"
                "所在平台当前不可用，已跳过（不会改发到其它平台）。"
                "请确认平台已启动或改用完整会话 UMO。"
            )
        return resolved, has_unavailable

    def _resolve_bare_sessions(self, raw: str, message_type: str) -> list[str]:
        """按指定类型补全裸 ID；无法确定时返回空列表。

        已知会话优先（保留 PrivateMessage / GuildMessage 等真实类型）。
        只有「完全没有已知会话、也没有离线目标」时才按唯一运行平台补全。
        """
        known, has_unavailable = self._lookup_known_sessions(raw, message_type)
        if known:
            return known
        if has_unavailable:
            # 该目标存在已知会话但平台离线：绝不猜测到其它平台。
            return []

        running = self._running_platform_ids()
        if len(running) != 1:
            logger.warning(
                f"[主动消息] 配置中的裸会话 ID「{raw}」无法确定所属平台"
                f"（当前运行平台 {len(running)} 个），已跳过。"
                "请改用完整会话 UMO。"
            )
            return []
        return [f"{running[0]}:{message_type}:{raw}"]

    def _resolve_bare_session_both_types(
        self, raw: str, session_type: str = ""
    ) -> list[str]:
        """按裸 ID 解析会话，且绝不为某一类型凭空猜测 UMO。

        Args:
            raw: 配置里的裸目标 ID。
            session_type: 该条目在迁移时记录的原会话类型（friend / group）；
                为空表示类型未知。

        与分别调用两次 _resolve_bare_sessions 的关键区别：
        后者在「裸 ID 只有群聊」时会先用私聊类型构造一个猜测的
        FriendMessage UMO，导致结果里混入一个并不存在的私聊会话。

        另外两点同样重要：
        - 任一类型存在离线目标时都不做平台猜测补全，否则会发到错的平台；
        - 类型未知且无已知会话时不默认按私聊补全，因为会话列表已合并，
          该条目可能原本来自群聊列表，猜成私聊会发错目标。
        """
        friend_known, friend_unavailable = self._lookup_known_sessions(
            raw, self._FRIEND_MESSAGE_TYPE
        )
        group_known, group_unavailable = self._lookup_known_sessions(
            raw, self._GROUP_MESSAGE_TYPE
        )

        resolved: list[str] = []
        for normalized in list(friend_known) + list(group_known):
            if normalized and normalized not in resolved:
                resolved.append(normalized)
        if resolved:
            return resolved

        if friend_unavailable or group_unavailable:
            # 存在离线目标：不补全，等平台恢复。
            return []

        if not session_type:
            # 类型未知且无任何已知会话：猜错类型的风险高于漏发，
            # 这里要求用户改用完整 UMO，不再默认当私聊。
            logger.warning(
                f"[主动消息] 配置中的裸会话 ID {raw} 无法确定会话类型，已跳过。"
                "请改用完整会话 UMO（例如 default:FriendMessage:123456789）。"
            )
            return []

        # 类型明确时才按唯一运行平台补全。
        message_type = (
            self._GROUP_MESSAGE_TYPE
            if session_type == "group"
            else self._FRIEND_MESSAGE_TYPE
        )
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
        defaults = plugin.config.get(SESSION_DEFAULTS_KEY, {})
        if not isinstance(defaults, dict) or not defaults.get(ENABLED_KEY, False):
            return []

        result: list[str] = []
        for session in defaults.get(SESSION_LIST_KEY, []) or []:
            if not isinstance(session, str) or not session:
                continue
            if plugin.session_service.parse_session_id(session):
                # 完整 UMO 归一后只对应一个会话。
                candidates = [plugin.session_service.normalize_session_id(session)]
            else:
                # 裸 ID 类型未知：先查两种类型的已知会话并合并。
                # 会话列表合并后已不记录来源类型，因此不传 session_type：
                # 类型无法确定时宁可跳过并提示，也不猜成私聊发错目标。
                candidates = self._resolve_bare_session_both_types(session)
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
        if not schedule_settings.get("quiet_hours_enabled", True):
            return "未启用"
        text = str(schedule_settings.get("quiet_hours", "") or "").strip()
        return text if text else "已启用但未设置时段"
