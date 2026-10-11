"""Web 控制台状态载荷构建。

由旧 core/web_admin_server.py 的状态/任务/会话/Markdown 逻辑等价搬迁而来。
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class StateBuilder:
    """构建运行状态、任务卡片、会话摘要与文档列表载荷。"""

    def __init__(
        self,
        plugin: Any,
        metadata_version: str,
        connection_counter: Any = None,
    ) -> None:
        self.plugin = plugin
        self._metadata_version = metadata_version
        # WebSocket 连接计数由实时通道提供，供状态载荷展示连接状态。
        self._connection_counter = connection_counter or (lambda: 0)

    # ------------------------------------------------------------------
    # 计时器元信息
    # ------------------------------------------------------------------
    def safe_timer_meta(self, timer: Any, now: float) -> dict[str, float | int | None]:
        """安全读取 asyncio 计时器的剩余时间与绝对目标时间。

        计时器可能已取消、已完成或实现不完整，统一用 None 表示“不可用”，
        避免异常向上冒泡导致状态载荷构建失败。
        """
        if timer is None:
            return {"remaining_seconds": None, "target_time": None}

        try:
            if getattr(timer, "cancelled", lambda: False)():
                return {"remaining_seconds": None, "target_time": None}
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

        when_method = getattr(timer, "when", None)
        if not callable(when_method):
            return {"remaining_seconds": None, "target_time": None}

        try:
            loop_time = when_method()
            loop = getattr(timer, "_loop", None)
            current_loop_time = loop.time() if loop else None
            if current_loop_time is None:
                return {"remaining_seconds": None, "target_time": None}

            remaining_precise = max(0.0, loop_time - current_loop_time)
            target_time = now + remaining_precise
            return {
                "remaining_seconds": max(0, int(math.ceil(remaining_precise))),
                "target_time": target_time,
            }
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

    def detect_session_category(self, session_id: str) -> str:
        """判断会话属于群聊还是私聊，用于前端展示分类。"""
        parsed = self.plugin.session_service.parse_session_id(session_id)
        if not parsed:
            lowered = str(session_id).lower()
            return "group" if "group" in lowered else "friend"

        _, msg_type, _ = parsed
        return "group" if "group" in msg_type.lower() else "friend"

    # ------------------------------------------------------------------
    # 计时器卡片
    # ------------------------------------------------------------------
    def collect_timer_cards(self, now: float) -> dict[str, list[dict[str, Any]]]:
        """收集自动触发与群聊沉默两类计时器卡片。

        群聊会话由沉默计时器驱动；若同一会话同时存在自动触发计时器，
        则以群聊卡片为准去重，避免重复展示。
        """
        plugin = self.plugin
        auto_cards: list[dict[str, Any]] = []
        group_cards: list[dict[str, Any]] = []
        # 记录已有群聊沉默计时器的会话，稍后据此过滤自动触发卡片。
        active_group_sessions = {
            str(session_id) for session_id in plugin.group_timers.keys()
        }

        for session_id, timer in list(plugin.auto_trigger_timers.items()):
            normalized_session_id = plugin.session_service.normalize_session_id(
                str(session_id)
            )
            # 该会话已由群聊沉默计时器接管，跳过自动触发卡片。
            if normalized_session_id in active_group_sessions:
                continue

            session_config = plugin.config_service.get_session_config(session_id) or {}
            session_data = plugin.session_data.get(session_id, {})
            auto_settings = session_config.get("auto_trigger_settings", {})
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            trigger_delay_minutes = int(
                auto_settings.get("auto_trigger_after_minutes", 0) or 0
            )
            trigger_delay_seconds = max(0, trigger_delay_minutes * 60)
            timer_meta = self.safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            started_at = max(plugin.plugin_start_time, now - trigger_delay_seconds)
            progress_percent = 0
            # 按“总窗口 - 剩余时间”估算已消耗进度，供前端进度条使用。
            if trigger_delay_seconds > 0 and remaining_seconds is not None:
                consumed = max(0, trigger_delay_seconds - remaining_seconds)
                progress_percent = max(
                    0, min(100, round((consumed / trigger_delay_seconds) * 100))
                )

            auto_cards.append(
                {
                    "session_id": normalized_session_id,
                    "session_name": plugin.session_service.get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": plugin.session_service.get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": self.detect_session_category(
                        normalized_session_id
                    ),
                    "source_mode": context_settings.get(
                        "source_mode", "conversation_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times", 0
                    ),
                    "timer_kind": "auto_trigger",
                    "title": "自动触发检测",
                    "status": "running" if remaining_seconds is not None else "unknown",
                    "remaining_seconds": remaining_seconds,
                    "target_time": target_time,
                    "started_at": started_at,
                    "window_seconds": trigger_delay_seconds,
                    "progress_percent": progress_percent,
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "auto_trigger_after_minutes": trigger_delay_minutes,
                }
            )

        # 群聊沉默计时器卡片（以用户最后发言时间为起点）。
        for session_id, timer in list(plugin.group_timers.items()):
            normalized_session_id = plugin.session_service.normalize_session_id(
                str(session_id)
            )
            session_config = (
                plugin.config_service.get_session_config(normalized_session_id) or {}
            )
            session_data = plugin.session_data.get(normalized_session_id, {})
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            idle_minutes = int(session_config.get("group_idle_trigger_minutes", 0) or 0)
            idle_seconds = max(0, idle_minutes * 60)
            timer_meta = self.safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            last_message_time = plugin.last_message_times.get(normalized_session_id, 0)
            temp_state = plugin.session_temp_state.get(normalized_session_id, {})
            last_user_time = (
                temp_state.get("last_user_time") or last_message_time or None
            )
            started_at = last_user_time or (
                now - max(0, idle_seconds - (remaining_seconds or 0))
            )
            progress_percent = 0
            if idle_seconds > 0 and remaining_seconds is not None:
                consumed = max(0, idle_seconds - remaining_seconds)
                progress_percent = max(
                    0, min(100, round((consumed / idle_seconds) * 100))
                )

            group_cards.append(
                {
                    "session_id": normalized_session_id,
                    "session_name": plugin.session_service.get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": plugin.session_service.get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": self.detect_session_category(
                        normalized_session_id
                    ),
                    "source_mode": context_settings.get(
                        "source_mode", "platform_message_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times", 0
                    ),
                    "timer_kind": "group_silence",
                    "title": "群沉默检测",
                    "status": "running" if remaining_seconds is not None else "unknown",
                    "remaining_seconds": remaining_seconds,
                    "target_time": target_time,
                    "started_at": started_at if started_at else None,
                    "window_seconds": idle_seconds,
                    "progress_percent": progress_percent,
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "group_idle_trigger_minutes": idle_minutes,
                    "last_message_time": last_message_time or None,
                    "last_user_time": last_user_time,
                    "is_live_group_timer": True,
                }
            )

        auto_cards.sort(
            key=lambda item: (
                item.get("remaining_seconds") is None,
                item.get("remaining_seconds") or 0,
                item["session_id"],
            )
        )
        group_cards.sort(
            key=lambda item: (
                item.get("remaining_seconds") is None,
                item.get("remaining_seconds") or 0,
                item["session_id"],
            )
        )
        return {
            "auto_trigger_cards": auto_cards,
            "group_timer_cards": group_cards,
        }

    # ------------------------------------------------------------------
    # 状态载荷
    # ------------------------------------------------------------------
    async def build_notification_payload(self) -> dict[str, Any]:
        """构建通知载荷（通知中心不可用时返回空结构）。"""
        notification_center = getattr(self.plugin, "notification_center", None)
        if not notification_center:
            return {
                "items": [],
                "meta": {
                    "unread_count": 0,
                    "last_sync_at": None,
                    "total_count": 0,
                },
            }
        return await notification_center.get_payload()

    def build_status_payload(self) -> dict[str, Any]:
        """构建运行状态载荷（版本、运行时长、任务计数与计时器卡片）。"""
        plugin = self.plugin
        now = time.time()
        uptime_sec = max(0, int(now - plugin.plugin_start_time))
        timer_cards = self.collect_timer_cards(now)

        return {
            "running": True,
            "version": getattr(plugin, "version", None)
            or getattr(plugin, "__version__", None)
            or self._metadata_version
            or "未知版本",
            "uptime_seconds": uptime_sec,
            "uptime": str(
                datetime.fromtimestamp(now)
                - datetime.fromtimestamp(plugin.plugin_start_time)
            ),
            "scheduler_running": bool(plugin.scheduler and plugin.scheduler.running),
            "sessions_count": len(plugin.session_data),
            "auto_trigger_timers": len(plugin.auto_trigger_timers),
            "group_timers": len(plugin.group_timers),
            "jobs_count": len(plugin.scheduler.get_jobs()) if plugin.scheduler else 0,
            "timer_cards_total": len(timer_cards["auto_trigger_cards"])
            + len(timer_cards["group_timer_cards"]),
            "auto_trigger_cards": timer_cards["auto_trigger_cards"],
            "group_timer_cards": timer_cards["group_timer_cards"],
            "ws_connections": self._connection_counter(),
            # 时间戳用于前端判断数据新鲜度或手动刷新完成时间。
            "timestamp": datetime.now().isoformat(),
        }

    def collect_jobs(self) -> list[dict[str, Any]]:
        """收集 APScheduler 中已注册的主动消息任务摘要。"""
        plugin = self.plugin
        if not plugin.scheduler:
            return []

        jobs = []
        for job in plugin.scheduler.get_jobs():
            session_id = str(job.id)
            session_data = plugin.session_data.get(session_id, {})
            session_config = plugin.config_service.get_session_config(session_id) or {}
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            auto_trigger_settings = session_config.get("auto_trigger_settings", {})
            jobs.append(
                {
                    "id": session_id,
                    "session_name": plugin.session_service.get_session_name(
                        session_id, session_config
                    ),
                    "session_display_name": plugin.session_service.get_session_display_name(
                        session_id, session_config
                    ),
                    "session_category": self.detect_session_category(session_id),
                    "source_mode": context_settings.get(
                        "source_mode", "conversation_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times",
                        auto_trigger_settings.get("max_unanswered_times", 0),
                    ),
                    "next_run_time": (
                        job.next_run_time.isoformat() if job.next_run_time else None
                    ),
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "manual_trigger_in_progress": session_id
                    in plugin.manual_trigger_sessions,
                    "next_trigger_time": session_data.get("next_trigger_time"),
                    "last_scheduled_at": session_data.get("last_scheduled_at"),
                    "last_schedule_min_interval_seconds": session_data.get(
                        "last_schedule_min_interval_seconds"
                    ),
                    "last_schedule_max_interval_seconds": session_data.get(
                        "last_schedule_max_interval_seconds"
                    ),
                    "last_schedule_random_interval_seconds": session_data.get(
                        "last_schedule_random_interval_seconds"
                    ),
                    "schedule_min_interval_minutes": schedule_settings.get(
                        "min_interval_minutes"
                    ),
                    "schedule_max_interval_minutes": schedule_settings.get(
                        "max_interval_minutes"
                    ),
                    "quiet_hours": schedule_settings.get("quiet_hours", ""),
                }
            )
        return jobs

    def list_known_sessions(self) -> list[str]:
        """汇总配置、会话数据与差异配置中出现过的所有会话标识。"""
        plugin = self.plugin
        sessions: set[str] = set()

        for scope_key in ("friend_settings", "group_settings"):
            cfg = plugin.config.get(scope_key, {})
            for session in cfg.get("session_list", []):
                if isinstance(session, str) and session:
                    sessions.add(plugin.session_service.normalize_session_id(session))

        sessions.update(plugin.session_data.keys())
        sessions.update(plugin.session_override_manager.list_sessions())
        return sorted(sessions)

    def list_known_session_summaries(self) -> list[dict[str, Any]]:
        """返回带展示信息的已知会话摘要（供 WS 实时推送使用）。"""
        plugin = self.plugin
        result: list[dict[str, Any]] = []
        for session in self.list_known_sessions():
            effective = plugin.config_service.get_session_config(session)
            result.append(
                {
                    "session": session,
                    "session_name": plugin.session_service.get_session_name(
                        session, effective
                    ),
                    "session_display_name": plugin.session_service.get_session_display_name(
                        session, effective
                    ),
                    "has_override": bool(
                        plugin.session_override_manager.get_override(session)
                    ),
                    "unanswered_count": plugin.session_data.get(session, {}).get(
                        "unanswered_count", 0
                    ),
                    "manual_trigger_in_progress": session
                    in plugin.manual_trigger_sessions,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Markdown 文档
    # ------------------------------------------------------------------
    def list_markdown_documents(self) -> list[dict[str, Any]]:
        """列出允许浏览的 Markdown 文档摘要。

        仅扫描插件根目录下的顶层 *.md 与 docs/ 目录，避免越权读取。
        """
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        plugin_root = Path(__file__).resolve().parents[2].resolve()
        docs_root = (plugin_root / "docs").resolve()

        # 允许浏览的文档集合：根目录同级的 Markdown 与 docs/ 下全部 Markdown。
        allowed_paths: list[Path] = []

        if plugin_root.exists():
            allowed_paths.extend(sorted(plugin_root.glob("*.md")))

        if docs_root.exists():
            allowed_paths.extend(sorted(docs_root.rglob("*.md")))

        for path in allowed_paths:
            if not path.is_file():
                continue

            try:
                relative_path = self.to_workspace_relative_path(path)
            except ValueError:
                continue

            normalized = relative_path.replace("\\", "/")
            if normalized in seen:
                continue
            seen.add(normalized)

            items.append(
                {
                    "path": normalized,
                    "title": path.stem,
                    "filename": path.name,
                    "category": "root"
                    if path.parent.resolve() == plugin_root
                    else path.parent.name,
                }
            )

        items.sort(
            key=lambda item: (
                0 if item["path"].count("/") == 0 else 1,
                item["path"].lower(),
            )
        )
        return items

    def resolve_markdown_document(self, raw_path: str) -> Path | None:
        """将前端请求的 Markdown 相对路径解析为插件目录中的受信任文件。"""
        normalized = str(raw_path or "").strip().replace("\\", "/")
        # 仅接受以 .md 结尾的相对路径。
        if not normalized or not normalized.lower().endswith(".md"):
            return None
        # 拒绝绝对路径与包含 .. 的路径穿越尝试。
        if (
            normalized.startswith("/")
            or normalized.startswith("../")
            or "/../" in normalized
        ):
            return None

        plugin_root = Path(__file__).resolve().parents[2].resolve()
        docs_root = (plugin_root / "docs").resolve()
        candidate = (plugin_root / normalized).resolve()

        if not candidate.is_file():
            return None

        try:
            relative_path = candidate.relative_to(plugin_root)
        except ValueError:
            return None

        if relative_path.parent == Path("."):
            return candidate

        try:
            candidate.relative_to(docs_root)
            return candidate
        except ValueError:
            return None

    def to_workspace_relative_path(self, path: Path) -> str:
        """将绝对路径转换为插件工作区内的相对路径。"""
        plugin_root = Path(__file__).resolve().parents[2].resolve()
        return str(path.resolve().relative_to(plugin_root)).replace("\\", "/")
