"""Web 控制台状态载荷构建。

由旧 core/web_admin_server.py 的状态/任务/会话/Markdown 逻辑等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..domain.config_keys import (
    CONTEXT_SETTINGS_KEY,
    GROUP_SILENCE_SETTINGS_KEY,
    SCHEDULE_SETTINGS_KEY,
    SESSION_DEFAULTS_KEY,
    SESSION_LIST_KEY,
    STARTUP_CHECK_SETTINGS_KEY,
)
from ..services.model_router import maybe_await


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
        # 人格摘要缓存：广播每 300ms 一次，状态与任务各解析一遍，
        # 逐会话读对话管理器是真实 I/O，短 TTL 足够新鲜又能避免重复读盘。
        # TTL 取 10 秒而不是 30 秒：会话级人格也可能在 /persona 等插件之外
        # 的路径改变，缓存只是广播合并窗口的补充，不能当成权威来源。
        self._persona_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._persona_cache_ttl = 10.0
        # Provider 清单缓存：(缓存时间, 清单)。模型列表拉取可能产生真实网络请求。
        self._provider_cache: tuple[float, list[dict[str, Any]]] | None = None
        # 缓存代次：每次失效都递增。解析在 await 期间可能跨越一次失效，
        # 写完前比对代次即可丢弃这次结果，避免把旧人格重新灌回缓存。
        self._persona_cache_generation = 0

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
            auto_settings = session_config.get(STARTUP_CHECK_SETTINGS_KEY, {})
            schedule_settings = session_config.get(SCHEDULE_SETTINGS_KEY, {})
            context_settings = session_config.get(CONTEXT_SETTINGS_KEY, {})
            trigger_delay_minutes = int(
                auto_settings.get("after_silence_minutes", 0) or 0
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
                    "quiet_hours": schedule_settings.get("quiet_hours", ""),
                    "quiet_hours_enabled": bool(
                        schedule_settings.get("quiet_hours_enabled", True)
                    ),
                    "timer_kind": "auto_trigger",
                    "title": "启动后静默检测",
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
            schedule_settings = session_config.get(SCHEDULE_SETTINGS_KEY, {})
            context_settings = session_config.get(CONTEXT_SETTINGS_KEY, {})
            idle_minutes = int(
                (session_config.get(GROUP_SILENCE_SETTINGS_KEY, {}) or {}).get(
                    "idle_minutes", 0
                )
                or 0
            )
            idle_seconds = max(0, idle_minutes * 60)
            timer_meta = self.safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            last_message_time = plugin.last_message_times.get(normalized_session_id, 0)
            temp_state = plugin.session_temp_state.get(normalized_session_id, {})
            # 优先级：人为重新计时的起点 > 真实发言时间 > 临时状态。
            # 重新计时不再改写 last_message_times，卡片必须从独立起点读，
            # 否则工期展示会跳回旧值。
            last_user_time = (
                plugin.silence_baselines.get(normalized_session_id)
                or last_message_time
                or temp_state.get("last_user_time")
                or None
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
                    "quiet_hours": schedule_settings.get("quiet_hours", ""),
                    "quiet_hours_enabled": bool(
                        schedule_settings.get("quiet_hours_enabled", True)
                    ),
                    "timer_kind": "group_silence",
                    "title": "群聊沉默判定",
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

    def invalidate_persona_cache(self, session_id: str | None = None) -> None:
        """清空人格摘要缓存；配置保存后调用，保证改人格立刻可见。

        同时递增代次：若此刻有解析正在 await，它写回缓存前会发现代次已变，
        从而放弃写入，否则失效会被这次迟到的结果撤销。
        """
        self._persona_cache_generation += 1
        if session_id is None:
            self._persona_cache.clear()
            return
        self._persona_cache.pop(str(session_id), None)

    async def build_persona_map(
        self, sessions: list[str], *, use_cache: bool = True
    ) -> dict[str, dict[str, Any]]:
        """批量解析会话生效人格，供状态 / 任务卡片直接标注。

        解析失败时返回空 dict，调用方按“未知”展示即可，不能因此让整个载荷失败。
        缓存命中时只补查缺失或过期的会话，避免同一轮广播重复解析。
        """
        unique_sessions = [s for s in dict.fromkeys(sessions) if s]
        if not unique_sessions:
            return {}

        result: dict[str, dict[str, Any]] = {}
        pending: list[str] = []
        now = time.time()
        generation = self._persona_cache_generation
        for session in unique_sessions:
            cached = self._persona_cache.get(session)
            if (
                use_cache
                and cached is not None
                and now - cached[0] < self._persona_cache_ttl
            ):
                result[session] = cached[1]
                continue
            pending.append(session)

        if not pending:
            return result

        try:
            resolved = await self.plugin.context_service.resolve_personas_for_sessions(
                pending
            )
        except Exception as e:
            # 解析整体失败时不写缓存：否则一次瞬时 I/O 故障会让面板在整个
            # TTL 内都显示“未知人格”，而且恢复后也不会自动刷新。
            logger.debug(f"[主动消息] 构建人格摘要映射失败喵: {e}")
            return result

        if not isinstance(resolved, dict):
            return result

        # 解析期间缓存被失效（配置保存）时，这批结果可能基于旧配置，直接丢弃。
        cache_valid = generation == self._persona_cache_generation

        for session in pending:
            persona = resolved.get(session, {})
            summary = persona if isinstance(persona, dict) else {}
            result[session] = summary
            # 单个会话解析失败的摘要（ok=False）同样不缓存，留待下次广播重试。
            if cache_valid and summary.get("ok", True):
                self._persona_cache[session] = (now, summary)
        return result

    async def build_realtime_payload(self) -> dict[str, Any]:
        """一次构建状态与任务载荷，共享同一份人格解析缓存。

        状态卡片与任务列表的会话高度重叠，分开构建会让同一会话在一轮广播里
        被解析两次；活跃群多时就是成倍的对话管理器读取。
        """
        status = await self.build_status_payload()
        jobs = self.collect_jobs()
        try:
            persona_map = await self.build_persona_map([job["id"] for job in jobs])
        except Exception as e:
            logger.debug(f"[主动消息] 构建任务人格摘要失败喵: {e}")
            persona_map = {}
        for job in jobs:
            job["persona"] = persona_map.get(job["id"], {})
        return {"status": status, "jobs": jobs}

    async def build_status_payload(self) -> dict[str, Any]:
        """构建运行状态载荷（版本、运行时长、任务计数与计时器卡片）。"""
        plugin = self.plugin
        now = time.time()
        uptime_sec = max(0, int(now - plugin.plugin_start_time))
        timer_cards = self.collect_timer_cards(now)
        # 计时器卡片的会话集合，用于一次性批量解析生效人格。
        persona_map = await self.build_persona_map(
            [card["session_id"] for card in timer_cards["auto_trigger_cards"]]
            + [card["session_id"] for card in timer_cards["group_timer_cards"]]
        )

        return {
            "running": True,
            # timer_snapshot_at 是 remaining_seconds 对应的服务器时刻。
            # 前端据此扣除「构建 + 传输」耗时，否则刷新时会莫名少掉数秒。
            "timer_snapshot_at": now,
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
            # 把人格信息挂到每张卡片上，前端无需再逐会话请求。
            "auto_trigger_cards": [
                {**card, "persona": persona_map.get(card["session_id"], {})}
                for card in timer_cards["auto_trigger_cards"]
            ],
            "group_timer_cards": [
                {**card, "persona": persona_map.get(card["session_id"], {})}
                for card in timer_cards["group_timer_cards"]
            ],
            "ws_connections": self._connection_counter(),
            # 时间戳用于前端判断数据新鲜度或手动刷新完成时间。
            "timestamp": datetime.now().isoformat(),
        }

    def collect_jobs(self) -> list[dict[str, Any]]:
        """收集 APScheduler 中已注册的主动消息任务摘要（不含人格，同步版）。"""
        return self._collect_jobs_sync()

    async def collect_jobs_async(self) -> list[dict[str, Any]]:
        """收集任务摘要并附带生效人格标注。"""
        jobs = self._collect_jobs_sync()
        persona_map = await self.build_persona_map([job["id"] for job in jobs])
        for job in jobs:
            job["persona"] = persona_map.get(job["id"], {})
        return jobs

    def _collect_jobs_sync(self) -> list[dict[str, Any]]:
        """任务摘要的同步构建实现，供异步版本复用。"""
        plugin = self.plugin
        if not plugin.scheduler:
            return []

        jobs = []
        for job in plugin.scheduler.get_jobs():
            session_id = str(job.id)
            session_data = plugin.session_data.get(session_id, {})
            session_config = plugin.config_service.get_session_config(session_id) or {}
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get(CONTEXT_SETTINGS_KEY, {})
            auto_trigger_settings = session_config.get(STARTUP_CHECK_SETTINGS_KEY, {})
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
                        "min_delay_minutes"
                    ),
                    "schedule_max_interval_minutes": schedule_settings.get(
                        "max_delay_minutes"
                    ),
                    "quiet_hours": schedule_settings.get("quiet_hours", ""),
                    "quiet_hours_enabled": bool(
                        schedule_settings.get("quiet_hours_enabled", True)
                    ),
                }
            )
        return jobs

    def list_known_sessions(self) -> list[str]:
        """汇总配置、会话数据与差异配置中出现过的所有会话标识。"""
        plugin = self.plugin
        sessions: set[str] = set()

        defaults = plugin.config.get(SESSION_DEFAULTS_KEY, {})
        for session in defaults.get(SESSION_LIST_KEY, []) or []:
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

    # ------------------------------------------------------------------
    # 可用模型清单
    # ------------------------------------------------------------------
    # 模型清单变化频率极低，而拉取可能触发真实 HTTP 请求，因此缓存时间取长一些。
    PROVIDER_CACHE_TTL = 60.0
    # 单个 Provider 的模型清单拉取上限：OpenAI 兼容端点会真的发一次请求，
    # 逐个串行等待会把配置页拖到十几秒，必须并发 + 单点超时。
    PROVIDER_MODELS_TIMEOUT = 3.0

    async def list_providers(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        """返回可用于配置选择的 Provider 清单（含模型列表）。

        模型列表是"尽力提供"：拿不到时返回空数组，前端会退化为只能手填模型名，
        不影响选择 Provider 本身。
        """
        now = time.time()
        cached = self._provider_cache
        if (
            not force_refresh
            and cached is not None
            and now - cached[0] < self.PROVIDER_CACHE_TTL
        ):
            return cached[1]

        providers = self._collect_provider_instances()
        if not providers:
            self._provider_cache = (now, [])
            return []

        models_list = await asyncio.gather(
            *(self._fetch_models(provider) for provider in providers.values()),
            return_exceptions=True,
        )

        items: list[dict[str, Any]] = []
        for (provider_id, provider), models in zip(providers.items(), models_list):
            if isinstance(models, BaseException):
                logger.debug(
                    f"[主动消息] 读取 Provider {provider_id} 的模型列表失败喵: {models}"
                )
                models = []
            items.append(
                {
                    "id": provider_id,
                    "name": self._provider_display_name(provider_id, provider),
                    "type": type(provider).__name__,
                    "default_model": self._provider_default_model(provider),
                    "models": models,
                }
            )

        items.sort(key=lambda item: item["id"].lower())
        self._provider_cache = (now, items)
        return items

    def invalidate_provider_cache(self) -> None:
        """清空模型清单缓存，配置保存后调用以拿到最新状态。"""
        self._provider_cache = None

    def _collect_provider_instances(self) -> dict[str, Any]:
        """收集 chat completion 类型的 Provider 实例。"""
        context = getattr(self.plugin, "context", None)
        if context is None:
            return {}
        getter = getattr(context, "get_all_providers", None)
        if not callable(getter):
            return {}
        try:
            insts = list(getter() or [])
        except Exception as e:
            logger.debug(f"[主动消息] 读取 Provider 列表失败喵: {e}")
            return {}

        result: dict[str, Any] = {}
        for provider in insts:
            config = getattr(provider, "provider_config", None)
            provider_id = ""
            if isinstance(config, dict):
                provider_id = str(config.get("id") or "").strip()
            if not provider_id:
                provider_id = str(getattr(provider, "provider_id", "") or "").strip()
            if not provider_id:
                continue
            result[provider_id] = provider
        return result

    @staticmethod
    def _provider_display_name(provider_id: str, provider: Any) -> str:
        """取 Provider 的可读名称，缺失时回落到 ID。"""
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            for key in ("name", "display_name", "type"):
                value = config.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return provider_id

    @staticmethod
    def _provider_default_model(provider: Any) -> str:
        """读取 Provider 当前默认模型名。"""
        getter = getattr(provider, "get_model", None)
        if callable(getter):
            try:
                value = getter()
                if isinstance(value, str) and value.strip():
                    return value.strip()
            except Exception:
                pass
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            for key in ("model", "model_name", "default_model"):
                value = config.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    @staticmethod
    async def _fetch_models(provider: Any) -> list[str]:
        """并发安全地拉取单个 Provider 的模型列表。"""
        getter = getattr(provider, "get_models", None)
        if not callable(getter):
            return []
        try:
            models = await asyncio.wait_for(
                maybe_await(getter()), timeout=StateBuilder.PROVIDER_MODELS_TIMEOUT
            )
        except Exception:
            # 超时与各类 Provider 自定义异常统一视为"拿不到模型列表"。
            return []
        if not isinstance(models, (list, tuple)):
            return []
        result: list[str] = []
        for item in models:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result
