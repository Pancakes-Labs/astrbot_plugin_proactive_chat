"""调度器与计时器服务。

由旧 core/task_scheduler.py 等价搬迁而来，调度、计时器与持久化逻辑保持不变。
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger

from ..domain.config_keys import (
    ENABLED_KEY,
    GROUP_SILENCE_SETTINGS_KEY,
    SESSION_DEFAULTS_KEY,
    SESSION_LIST_KEY,
    STARTUP_CHECK_SETTINGS_KEY,
)
from ..domain.session_key import is_friend_type
from ..domain.unanswered_counter import (
    REASON_MISSING_WITH_HISTORY,
    UNANSWERED_COUNT_KEY,
    UNTRUSTED_MARK_KEY,
    is_unanswered_limit_enabled,
    mark_untrusted,
    reaches_max_unanswered,
    resolve_unanswered_state,
)


class SchedulerService:
    """定时任务、自动触发与沉默计时服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        # 计时器代次表：旧回调据此判断自己是否已被替换。
        self._timer_generations: dict[str, int] = {}
        # Web 端 finish/reset 会同时操作计时器与任务，按会话串行更安全。
        self._timer_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 内部辅助：Web 控制台状态广播
    # ------------------------------------------------------------------
    async def _notify_web_console(self, reason: str) -> None:
        """通知 Web 控制台刷新计时器卡片。

        计时器到期后若不广播，前端只能靠慢轮询发现，卡片会长期残留过期状态。
        广播异常一律吞掉，不能影响调度主流程。
        """
        web = getattr(self.plugin, "web_admin_server", None)
        if not web:
            return
        try:
            await web._broadcast_update(reason)
        except Exception as e:
            logger.debug(f"[主动消息] 广播 {reason} 状态失败喵: {e}")

    # ------------------------------------------------------------------
    # 自动触发
    # ------------------------------------------------------------------
    # 计时器代次：每次重建都递增，旧回调只认自己那一代，避免已被替换的
    # 回调在新的计时器上生效（重置后提前触发或误删卡片）。
    def _next_timer_generation(self, key: str) -> int:
        current = self._timer_generations.get(key, 0) + 1
        self._timer_generations[key] = current
        return current

    def _current_timer_generation(self, key: str) -> int:
        return self._timer_generations.get(key, 0)

    @staticmethod
    def _section_dict(section: Any, key: str) -> dict[str, Any]:
        """读取配置分组并保证返回 dict。

        管理端接口允许把分组保存成 null，直接链式 .get() 会抛
        AttributeError 并中断整轮排期，因此统一在这里做空值归一。
        """
        if not isinstance(section, dict):
            return {}
        value = section.get(key)
        return value if isinstance(value, dict) else {}

    async def setup_auto_trigger(self, session_id: str, silent: bool = False) -> None:
        """为指定会话设置自动主动消息触发器。"""
        plugin = self.plugin
        session_id = plugin.session_service.normalize_session_id(session_id)
        session_config = plugin.config_service.get_session_config(session_id)
        if not session_config:
            return

        auto_trigger_settings = self._section_dict(
            session_config, STARTUP_CHECK_SETTINGS_KEY
        )
        if not auto_trigger_settings.get("enabled", False):
            logger.debug(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} 未启用启动后检测喵。"
            )
            return

        auto_trigger_minutes = auto_trigger_settings.get("after_silence_minutes", 5)
        if auto_trigger_minutes <= 0:
            logger.debug(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} 的自动触发时间设置为0，禁用自动触发喵。"
            )
            return

        if session_id in plugin.auto_trigger_timers:
            try:
                plugin.auto_trigger_timers[session_id].cancel()
                logger.debug(
                    f"[主动消息] 已取消 {plugin.session_service.get_session_log_str(session_id, session_config)} 现有的自动触发计时器喵。"
                )
            except Exception as e:
                logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")
            finally:
                del plugin.auto_trigger_timers[session_id]

        generation = self._next_timer_generation(f"auto:{session_id}")

        def _auto_trigger_callback(captured_session_id=session_id, token=generation):
            # 已被重建的计时器回调直接作废，否则会作用到新的计时器上。
            if self._current_timer_generation(f"auto:{captured_session_id}") != token:
                return
            plugin._track_task(
                asyncio.create_task(
                    self.handle_auto_trigger_callback(
                        captured_session_id, auto_trigger_minutes, token
                    )
                )
            )

        try:
            loop = asyncio.get_running_loop()
            delay_seconds = auto_trigger_minutes * 60
            plugin.auto_trigger_timers[session_id] = loop.call_later(
                delay_seconds, _auto_trigger_callback
            )
            if not silent:
                logger.info(
                    f"[主动消息] 已为 {plugin.session_service.get_session_log_str(session_id, session_config)} 设置自动主动消息触发器喵，"
                    f"将在 {auto_trigger_minutes} 分钟后检查是否需要自动触发喵。"
                )
        except Exception as e:
            logger.error(f"[主动消息] 设置自动触发计时器失败喵: {e}")

    def iter_related_auto_trigger_keys(self, session_id: str) -> list[str]:
        """列出与目标会话相关的自动触发计时器键（含同目标历史键）。"""
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        parsed = plugin.session_service.parse_session_id(normalized_session_id)
        target_scope = None
        if parsed:
            _, msg_type, target_id = parsed
            target_scope = (is_friend_type(msg_type), target_id)

        matched: list[str] = []
        for timer_key in list(plugin.auto_trigger_timers.keys()):
            if timer_key == normalized_session_id:
                matched.append(timer_key)
                continue
            if target_scope is None:
                continue
            key_parsed = plugin.session_service.parse_session_id(str(timer_key))
            if not key_parsed:
                continue
            _, key_type, key_target = key_parsed
            if (
                is_friend_type(key_type) == target_scope[0]
                and key_target == target_scope[1]
            ):
                matched.append(timer_key)
        return matched

    async def cancel_auto_trigger(self, session_id: str) -> bool:
        """取消指定会话的自动主动消息触发器（含同目标历史键）。"""
        plugin = self.plugin
        cancelled = False
        for timer_key in self.iter_related_auto_trigger_keys(session_id):
            try:
                plugin.auto_trigger_timers[timer_key].cancel()
                cancelled = True
                logger.info(
                    f"[主动消息] 已取消 {plugin.session_service.get_session_log_str(timer_key)} 的自动触发计时器喵。"
                )
            except Exception as e:
                logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")
            finally:
                plugin.auto_trigger_timers.pop(timer_key, None)
        return cancelled

    async def cancel_all_related_auto_triggers(self, session_id: str) -> bool:
        """取消指定会话及其同目标历史键的自动触发器。"""
        return await self.cancel_auto_trigger(session_id)

    # ------------------------------------------------------------------
    # 持久化任务校验与清理
    # ------------------------------------------------------------------
    def is_persisted_task_still_valid(
        self,
        session_id: str,
        session_info: dict | None,
        current_time: float | None = None,
    ) -> bool:
        """判断持久化任务是否仍然有效且可恢复。"""
        if not isinstance(session_info, dict):
            return False

        session_config = self.plugin.config_service.get_session_config(session_id)
        if not session_config or not session_config.get(ENABLED_KEY, False):
            return False

        next_trigger = session_info.get("next_trigger_time")
        if not isinstance(next_trigger, (int, float)):
            return False

        check_time = current_time if current_time is not None else time.time()
        return check_time < (next_trigger + 60)

    def clear_session_schedule_state(
        self,
        session_id: str,
        *,
        keep_unanswered_count: bool = True,
        keep_last_message_time: bool = True,
        keep_self_id: bool = True,
    ) -> bool:
        """清理会话上的调度持久化字段，避免残留幽灵任务状态。"""
        session_info = self.plugin.session_data.get(session_id)
        if not isinstance(session_info, dict):
            return False

        protected_keys = set()
        if keep_unanswered_count:
            protected_keys.add("unanswered_count")
        if keep_last_message_time:
            protected_keys.add("last_message_time")
        if keep_self_id:
            protected_keys.add("self_id")

        schedule_keys = {
            "next_trigger_time",
            "last_scheduled_at",
            "last_schedule_min_interval_seconds",
            "last_schedule_max_interval_seconds",
            "last_schedule_random_interval_seconds",
        }

        changed = False
        for key in schedule_keys:
            if key in protected_keys:
                continue
            if key in session_info:
                del session_info[key]
                changed = True

        return changed

    # ------------------------------------------------------------------
    # 调度时间计算（全项目唯一来源）
    # ------------------------------------------------------------------
    def compute_next_trigger(
        self, schedule_conf: dict
    ) -> tuple[datetime, float, int, int, int, float]:
        """计算下一次主动消息的触发时间。

        由“最小/最大间隔分钟数”推导随机间隔并生成执行时间。
        私聊调度与自动触发共用本方法，确保时间语义只在一处维护。

        Returns:
            (run_date, scheduled_at, min_interval, max_interval,
             random_interval, next_trigger_time)
        """
        plugin = self.plugin
        min_interval = int(schedule_conf.get("min_delay_minutes", 30)) * 60
        max_interval = max(
            min_interval,
            int(schedule_conf.get("max_delay_minutes", 600)) * 60,
        )
        random_interval = random.randint(min_interval, max_interval)
        scheduled_at = time.time()
        next_trigger_time = scheduled_at + random_interval
        run_date = datetime.fromtimestamp(next_trigger_time, tz=plugin.timezone)
        return (
            run_date,
            scheduled_at,
            min_interval,
            max_interval,
            random_interval,
            next_trigger_time,
        )

    def purge_related_jobs(self, session_id: str) -> None:
        """清理同一目标但不同 UMO 的调度任务，防止幽灵任务。"""
        plugin = self.plugin
        parsed = plugin.session_service.parse_session_id(session_id)
        if not parsed:
            return

        _, msg_type, target_id = parsed
        is_friend = is_friend_type(msg_type)

        for job in plugin.scheduler.get_jobs():
            job_id = str(job.id)
            job_parsed = plugin.session_service.parse_session_id(job_id)
            if not job_parsed:
                continue
            _, job_type, job_target = job_parsed
            if is_friend_type(job_type) == is_friend and job_target == target_id:
                try:
                    plugin.scheduler.remove_job(job.id)
                except Exception:
                    pass

    def add_chat_job(self, run_session_id: str, run_date: datetime) -> None:
        """规范化 session_id，清理同目标历史任务，并注册一个新的定时 job。"""
        plugin = self.plugin
        normalized = plugin.session_service.normalize_session_id(run_session_id)
        self.purge_related_jobs(normalized)
        plugin.scheduler.add_job(
            plugin.check_and_chat,
            "date",
            run_date=run_date,
            args=[normalized],
            id=normalized,
            replace_existing=True,
            misfire_grace_time=60,
        )

    def has_related_persisted_task(self, session_id: str) -> bool:
        """判断同一目标是否存在仍可恢复的持久化任务（避免重复触发）。"""
        plugin = self.plugin
        parsed = plugin.session_service.parse_session_id(session_id)
        if not parsed:
            return False

        _, msg_type, target_id = parsed
        is_friend = is_friend_type(msg_type)
        current_time = time.time()

        for existing_id, session_info in list(plugin.session_data.items()):
            existing_parsed = plugin.session_service.parse_session_id(existing_id)
            if not existing_parsed:
                continue
            _, existing_type, existing_target = existing_parsed
            if (
                is_friend_type(existing_type) == is_friend
                and existing_target == target_id
                and self.is_persisted_task_still_valid(
                    existing_id, session_info, current_time=current_time
                )
            ):
                return True

        return False

    def resolve_session_id_for_config(
        self, session_id: str, session_config: dict
    ) -> str:
        """将配置中的会话标识解析为完整 UMO。

        配置只接受完整 UMO；解析失败时返回空串，由调用方按无效配置跳过。
        """
        if self.plugin.session_service.parse_session_id(session_id):
            return session_id
        logger.warning(
            f"[主动消息] 会话 {session_id} 不是完整 UMO，已跳过。"
            "请改用完整会话 UMO（例如 default:FriendMessage:123456789）。"
        )
        return ""

    # ------------------------------------------------------------------
    # 启动初始化
    # ------------------------------------------------------------------
    async def setup_auto_triggers_for_enabled_sessions(self) -> None:
        """为所有启用了自动触发功能的会话设置自动主动消息触发器。

        分别遍历私聊与群聊配置的 session_list，逐项尝试创建触发器并统计结果。
        """
        plugin = self.plugin
        logger.info("[主动消息] 开始检查并设置自动主动消息触发器喵...")

        # 各类跳过原因分别计数，用于汇总日志。
        auto_trigger_count = 0
        skipped_existing = 0
        skipped_invalid = 0
        skipped_disabled = 0
        skipped_max_unanswered = 0
        skipped_untrusted = 0

        # 合并后只有一个会话分组，私聊与群聊统一遍历。
        defaults = plugin.config.get(SESSION_DEFAULTS_KEY, {})
        if defaults.get(ENABLED_KEY, False):
            for session_id in defaults.get(SESSION_LIST_KEY, []) or []:
                result = await self.setup_auto_trigger_for_session_config(
                    defaults, session_id
                )
                if result == "created":
                    auto_trigger_count += 1
                elif result == "existing":
                    skipped_existing += 1
                elif result == "invalid":
                    skipped_invalid += 1
                elif result == "disabled":
                    skipped_disabled += 1
                elif result == "max_unanswered":
                    skipped_max_unanswered += 1
                elif result == "untrusted_count":
                    skipped_untrusted += 1

        # POST /api/config 允许把分组写成 null；直接链式 .get() 会抛异常，
        # 让全局开关命令后续的排期整体中断，这里统一归一化为空字典。
        startup_check = self._section_dict(defaults, STARTUP_CHECK_SETTINGS_KEY)
        has_auto_trigger_config = bool(startup_check.get("enabled", False))

        if auto_trigger_count == 0:
            if has_auto_trigger_config:
                reasons = []
                if skipped_existing:
                    reasons.append(f"{skipped_existing} 个会话已有持久化任务")
                if skipped_invalid:
                    reasons.append(f"{skipped_invalid} 个会话无效或未配置")
                if skipped_disabled:
                    reasons.append(f"{skipped_disabled} 个会话未启用自动触发")
                if skipped_max_unanswered:
                    reasons.append(
                        f"{skipped_max_unanswered} 个会话已达到未回复次数上限"
                    )
                if skipped_untrusted:
                    reasons.append(
                        f"{skipped_untrusted} 个会话未回复计数不可信，保守跳过"
                    )
                reason_str = "，".join(reasons) if reasons else "未发现可设置的会话"
                logger.info(
                    f"[主动消息] 检测到自动主动消息配置，但没有需要设置的触发器喵（{reason_str}）。"
                )
            else:
                logger.info("[主动消息] 没有会话启用自动主动消息功能喵。")
        else:
            logger.info(
                f"[主动消息] 已为 {auto_trigger_count} 个会话设置自动主动消息触发器喵。"
                f"（跳过：已有任务 {skipped_existing}，无效 {skipped_invalid}，未启用 {skipped_disabled}，"
                f"已达未回复上限 {skipped_max_unanswered}，计数不可信 {skipped_untrusted}）"
            )

    async def setup_auto_trigger_for_session_config(
        self, settings: dict, session_id: str
    ) -> str:
        """为指定会话配置设置自动触发器。

        返回结果标记：invalid(配置无效) / disabled(未启用) /
        existing(已有持久化任务) / max_unanswered(已达上限) / created(已创建)。
        """
        plugin = self.plugin
        session_config = plugin.config_service.get_session_config(session_id)
        if not session_config or not session_config.get(ENABLED_KEY, False):
            return "invalid"

        resolved_session_id = self.resolve_session_id_for_config(
            session_id, session_config
        )
        if not resolved_session_id:
            return "invalid"

        auto_trigger_settings = self._section_dict(
            session_config, STARTUP_CHECK_SETTINGS_KEY
        )
        if not auto_trigger_settings.get("enabled", False):
            logger.debug(
                f"[主动消息] {plugin.session_service.get_session_log_str(resolved_session_id)} 未启用启动后检测喵。"
            )
            return "disabled"

        if self.has_related_persisted_task(resolved_session_id):
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(resolved_session_id)} 已存在持久化的主动消息任务喵，"
                f"跳过自动触发器设置以避免冲突喵。"
            )
            return "existing"

        schedule_conf = session_config.get("schedule_settings", {})
        max_unanswered = schedule_conf.get("max_unanswered_times", 3)
        state = resolve_unanswered_state(plugin.session_data.get(resolved_session_id))
        # 计数缺失但存在历史调度痕迹时无法证明未达上限，保守跳过，
        # 避免数据丢失后立刻为已达上限的会话重新排期。
        if state.reason == REASON_MISSING_WITH_HISTORY and is_unanswered_limit_enabled(
            max_unanswered
        ):
            logger.warning(
                f"[主动消息] {plugin.session_service.get_session_log_str(resolved_session_id, session_config)} 的未回复计数缺失，"
                "但存在历史调度痕迹，跳过初始化自动触发器设置以避免误排期喵。"
            )
            return "untrusted_count"
        if reaches_max_unanswered(state, max_unanswered):
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(resolved_session_id, session_config)} 的未回复次数 ({state.count}) "
                f"已达到上限 ({max_unanswered})，跳过初始化自动触发器设置喵。"
            )
            return "max_unanswered"

        logger.debug(
            f"[主动消息] 正在为 {plugin.session_service.get_session_log_str(resolved_session_id)} 设置自动触发器喵。"
        )
        auto_trigger_minutes = auto_trigger_settings.get("after_silence_minutes", 5)
        logger.info(
            f"[主动消息] 已为 {plugin.session_service.get_session_log_str(resolved_session_id)} 设置启动检测计时器喵，"
            f"将在 {auto_trigger_minutes} 分钟静默后安排一次主动消息喵。"
        )
        await self.setup_auto_trigger(resolved_session_id, silent=True)
        return "created"

    async def init_jobs_from_data(self) -> None:
        """从已加载的 session_data 中恢复定时任务。

        恢复前先清理无效数据与残留调度状态；已过期或无效的持久化任务
        会被清除而非恢复，避免产生幽灵任务。
        """
        plugin = self.plugin
        restored_count = 0
        cleaned_runtime_state = 0
        current_time = time.time()

        logger.info(
            f"[主动消息] 开始从数据恢复定时任务喵，当前时间: {datetime.fromtimestamp(current_time)}"
        )

        cleaned_count = self.plugin.storage_service.cleanup_invalid_session_data()
        if cleaned_count > 0:
            logger.info(f"[主动消息] 清理了 {cleaned_count} 个无效的会话数据条目喵。")
            async with plugin.data_lock:
                await plugin.storage_service.save_data()

        logger.debug(f"[主动消息] 会话数据条目数: {len(plugin.session_data)}")

        # 逐会话判断是否具备恢复条件。
        for session_id, session_info in list(plugin.session_data.items()):
            session_config = plugin.config_service.get_session_config(session_id)
            # 配置缺失或已禁用：清理残留调度状态后跳过。
            if not session_config or not session_config.get(ENABLED_KEY, False):
                if self.clear_session_schedule_state(session_id):
                    cleaned_runtime_state += 1
                    logger.info(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} 的配置无效或已禁用，已清理残留调度状态喵。"
                    )
                continue

            next_trigger = session_info.get("next_trigger_time")
            if not next_trigger:
                logger.debug(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} 没有next_trigger_time，跳过喵"
                )
                continue

            if not self.is_persisted_task_still_valid(
                session_id, session_info, current_time=current_time
            ):
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} 的持久化任务已过期或无效，清理后跳过恢复喵。"
                )
                if self.clear_session_schedule_state(session_id):
                    cleaned_runtime_state += 1
                    logger.debug(
                        f"[主动消息] 已清理 {plugin.session_service.get_session_log_str(session_id, session_config)} 的过期持久化状态喵。"
                    )
                continue

            try:
                run_date = datetime.fromtimestamp(next_trigger, tz=plugin.timezone)
                normalized_session_id = plugin.session_service.normalize_session_id(
                    session_id
                )
                existing_job = plugin.scheduler.get_job(normalized_session_id)
                if existing_job:
                    logger.debug(
                        f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的任务已存在，跳过恢复喵。"
                    )
                    continue

                self.add_chat_job(normalized_session_id, run_date)
                logger.info(
                    f"[主动消息] 已成功从文件恢复任务喵: {plugin.session_service.get_session_log_str(session_id, session_config)}, 执行时间: {run_date} 喵"
                )
                restored_count += 1
            except Exception as e:
                logger.error(
                    f"[主动消息] 添加 {plugin.session_service.get_session_log_str(session_id, session_config)} 的恢复任务到调度器时失败喵: {e}"
                )
                if self.clear_session_schedule_state(session_id):
                    cleaned_runtime_state += 1
                    logger.warning(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} 的恢复任务创建失败，已清理残留持久化状态喵。"
                    )

        if cleaned_runtime_state > 0:
            async with plugin.data_lock:
                await plugin.storage_service.save_data()

        logger.info(
            f"[主动消息] 任务恢复检查完成，共恢复 {restored_count} 个定时任务喵。"
        )
        if cleaned_runtime_state > 0:
            logger.info(
                f"[主动消息] 启动恢复阶段额外清理了 {cleaned_runtime_state} 个残留调度状态喵。"
            )
        if restored_count == 0:
            logger.info("[主动消息] 没有需要恢复的定时任务喵。")

    async def schedule_next_chat_and_save(
        self, session_id: str, reset_counter: bool = False
    ) -> None:
        """安排下一次主动聊天并立即将状态持久化到文件。

        全过程在 data_lock 内完成：迁移会话键 -> 重置计数(可选) ->
        计算触发时间 -> 注册任务 -> 写入调度字段 -> 落盘。
        """
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            return

        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        session_config = plugin.config_service.get_session_config(normalized_session_id)
        if not session_config:
            return

        schedule_conf = session_config.get("schedule_settings", {})

        async with plugin.data_lock:
            # 若传入的是非规范键，先把其数据迁移到规范键下。
            if (
                normalized_session_id != session_id
                and session_id in plugin.session_data
            ):
                existing_payload = plugin.session_data.get(session_id, {})
                plugin.session_data.setdefault(normalized_session_id, {}).update(
                    existing_payload
                )
                del plugin.session_data[session_id]

            if normalized_session_id != session_id:
                try:
                    plugin.scheduler.remove_job(session_id)
                except Exception:
                    pass

            if reset_counter:
                reset_payload = plugin.session_data.setdefault(
                    normalized_session_id, {}
                )
                reset_payload["unanswered_count"] = 0
                # 计数被显式重置为 0，此前的「不可信」推断随之失效。
                reset_payload.pop(UNTRUSTED_MARK_KEY, None)

            (
                run_date,
                scheduled_at,
                min_interval,
                max_interval,
                random_interval,
                next_trigger_time,
            ) = self.compute_next_trigger(schedule_conf)

            self.add_chat_job(normalized_session_id, run_date)

            session_payload = plugin.session_data.setdefault(normalized_session_id, {})
            session_payload["next_trigger_time"] = next_trigger_time
            session_payload["last_scheduled_at"] = scheduled_at
            session_payload["last_schedule_min_interval_seconds"] = min_interval
            session_payload["last_schedule_max_interval_seconds"] = max_interval
            session_payload["last_schedule_random_interval_seconds"] = random_interval
            logger.info(
                f"[主动消息] 已为 {plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 安排下一次主动消息喵，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵。"
            )

            await plugin.storage_service.save_data()

    # ------------------------------------------------------------------
    # 群聊沉默计时
    # ------------------------------------------------------------------
    async def reset_group_silence_timer(
        self, session_id: str, *, fresh_start: bool = False
    ) -> None:
        """重置指定群聊的沉默倒计时。

        先取消旧计时器（含原始键与规范键），再按配置的静默分钟数重新计时。
        fresh_start=True 表示由「重新计时」主动重建：此时沉默起点改为当前时刻，
        写进 silence_baselines，而不是覆盖用户的真实发言时间。
        """
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        session_config = plugin.config_service.get_session_config(normalized_session_id)
        if not session_config or not session_config.get(ENABLED_KEY, False):
            return

        for timer_key in [session_id, normalized_session_id]:
            if timer_key in plugin.group_timers:
                try:
                    plugin.group_timers[timer_key].cancel()
                except Exception as e:
                    logger.warning(
                        f"[主动消息] 取消 {plugin.session_service.get_session_log_str(timer_key, session_config)} 的旧计时器时出错喵: {e}"
                    )
                finally:
                    del plugin.group_timers[timer_key]

        if fresh_start:
            # 独立起点：卡片与日志都从这里读，不动 last_message_times。
            plugin.silence_baselines[normalized_session_id] = time.time()
        else:
            # 真实的用户发言：起点随消息时间走，清掉人为起点以免继续生效。
            plugin.silence_baselines.pop(normalized_session_id, None)

        idle_minutes = (session_config.get(GROUP_SILENCE_SETTINGS_KEY, {}) or {}).get(
            "idle_minutes", 10
        )
        generation = self._next_timer_generation(f"silence:{normalized_session_id}")

        def _schedule_callback(
            captured_session_id=normalized_session_id, token=generation
        ):
            # 重置会重建计时器并递增代次，旧回调据此自动失效。
            if (
                self._current_timer_generation(f"silence:{captured_session_id}")
                != token
            ):
                return
            plugin._track_task(
                asyncio.create_task(
                    self.handle_group_silence_callback(
                        captured_session_id, idle_minutes, timer_generation=token
                    )
                )
            )

        try:
            loop = asyncio.get_running_loop()
            plugin.group_timers[normalized_session_id] = loop.call_later(
                idle_minutes * 60, _schedule_callback
            )
        except Exception as e:
            logger.error(f"[主动消息] 设置沉默倒计时失败喵: {e}")

    # ------------------------------------------------------------------
    # Web 管理端：在线结束 / 重置计时器
    # ------------------------------------------------------------------
    def _session_timer_lock(self, session_id: str) -> asyncio.Lock:
        """取该会话的计时器操作锁，保证 finish/reset 不交错执行。"""
        lock = self._timer_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._timer_locks[session_id] = lock
        return lock

    async def finish_timer(
        self, session_id: str, timer_kind: str = ""
    ) -> dict[str, Any]:
        """结束计时器但不发送消息。

        与真实到点回调的区别：不做触发条件校验，直接把计时器摘除，再按正常
        流程登记下一次主动消息任务，使卡片消失、任务出现在任务管理页。
        """
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        # timer_kind 为空时两类都尝试，避免前端漏传导致点了没反应。
        kinds = (
            [timer_kind]
            if timer_kind in {"group_silence", "auto_trigger"}
            else ["group_silence", "auto_trigger"]
        )

        async with self._session_timer_lock(normalized_session_id):
            # 先确认确有计时器可结束，避免“没东西可结束”却排了新任务。
            has_group_timer = any(
                key in plugin.group_timers
                for key in (session_id, normalized_session_id)
            )
            has_auto_timer = bool(
                self.iter_related_auto_trigger_keys(normalized_session_id)
            )
            finished = ("group_silence" in kinds and has_group_timer) or (
                "auto_trigger" in kinds and has_auto_timer
            )
            if not finished:
                return {"ok": False, "reason": "not_running"}

            session_config = plugin.config_service.get_session_config(
                normalized_session_id
            )
            can_reschedule = bool(
                session_config and session_config.get(ENABLED_KEY, False)
            )

            # 先登记替代任务再摘除计时器：排期失败时原计时器仍可继续工作，
            # 不会出现“旧计时器已消失、新任务也不存在”的空窗。
            if can_reschedule:
                try:
                    await self.schedule_next_chat_and_save(
                        normalized_session_id, reset_counter=False
                    )
                except Exception as e:  # noqa: BLE001 - 需保留原计时器
                    logger.error(
                        f"[主动消息] 结束计时器时登记后续任务失败，保留原计时器喵: {e}"
                    )
                    return {"ok": False, "reason": "reschedule_failed"}

            # 只结束其中之一也必须摘掉另一类计时器：
            # 残留的兄弟计时器到点后会再排一次任务，把刚登记的时间覆盖掉。
            # 代次递增确保它的回调不会继续生效。
            for timer_key in (session_id, normalized_session_id):
                timer = plugin.group_timers.pop(timer_key, None)
                if timer is None:
                    continue
                self._next_timer_generation(f"silence:{timer_key}")
                try:
                    timer.cancel()
                except Exception as e:
                    logger.warning(f"[主动消息] 取消沉默计时器时出错喵: {e}")

            if has_auto_timer:
                await self.cancel_auto_trigger(normalized_session_id)
                self._next_timer_generation(f"auto:{normalized_session_id}")

            if can_reschedule:
                logger.info(
                    f"[主动消息] Web 管理端已结束 {plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的计时器并转入调度任务喵。"
                )

        await self._notify_web_console("timer_finished")
        return {"ok": True, "scheduled": can_reschedule}

    async def reset_timer(
        self, session_id: str, timer_kind: str = ""
    ) -> dict[str, Any]:
        """按当前配置重新开始计时（不发送消息、不改动未回复计数）。

        两类计时器的“起点”语义完全不同，必须分开处理：
        群沉默以“用户最后发言时间”为起点，自动触发以“一直无消息”为前提。
        自动触发不能复用 last_message_times，那是用户真实发言时间，
        清零会让后续判定误以为用户从未说过话。
        """
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        session_config = plugin.config_service.get_session_config(normalized_session_id)
        if not session_config or not session_config.get(ENABLED_KEY, False):
            return {"ok": False, "reason": "disabled"}

        kinds = (
            [timer_kind]
            if timer_kind in {"group_silence", "auto_trigger"}
            else ["group_silence", "auto_trigger"]
        )

        async with self._session_timer_lock(normalized_session_id):
            reset_done = False
            # 未显式指定类型时，只重置真实存在的那一类，凭空造计时器会多出卡片。
            auto_result = ""

            if "group_silence" in kinds:
                has_group_timer = (
                    normalized_session_id in plugin.group_timers
                    or session_id in plugin.group_timers
                )
                idle_minutes = int(
                    (session_config.get(GROUP_SILENCE_SETTINGS_KEY, {}) or {}).get(
                        "idle_minutes", 0
                    )
                    or 0
                )
                # 未指定类型（用户点“重新计时”）时要求确有计时器；
                # 显式指定 group_silence 时允许按配置重建。
                should_reset_group = (
                    timer_kind == "group_silence" and idle_minutes > 0
                ) or has_group_timer
                if should_reset_group:
                    # 不能改写 last_message_times：那是用户的真实发言时间，
                    # 抬高它会让自动触发判定认为“刚有新消息”而跳过，
                    # 也会让 REVIEW 阶段把本次生成误判成新消息而丢弃结果。
                    # 重新计时的起点单独记录，卡片优先读取这个值。
                    await self.reset_group_silence_timer(
                        normalized_session_id, fresh_start=True
                    )
                    reset_done = True

            if "auto_trigger" in kinds:
                auto_settings = self._section_dict(
                    session_config, STARTUP_CHECK_SETTINGS_KEY
                )
                had_auto_timer = bool(
                    self.iter_related_auto_trigger_keys(normalized_session_id)
                )
                if had_auto_timer or auto_settings.get("enabled", False):
                    # 先确认能否重建，再取消旧计时器：与 finish_timer 同序。
                    # 否则 existing / max_unanswered 时旧计时器已丢、卡片也消失，
                    # 接口却只返回一个 400。
                    if not auto_settings.get("enabled", False):
                        auto_result = "disabled"
                    elif self.has_related_persisted_task(normalized_session_id):
                        auto_result = "existing"
                    else:
                        schedule_conf = session_config.get("schedule_settings", {})
                        max_unanswered = schedule_conf.get("max_unanswered_times", 3)
                        state = resolve_unanswered_state(
                            plugin.session_data.get(normalized_session_id)
                        )
                        if state.reason == REASON_MISSING_WITH_HISTORY and (
                            is_unanswered_limit_enabled(max_unanswered)
                        ):
                            auto_result = "untrusted_count"
                        elif reaches_max_unanswered(state, max_unanswered):
                            auto_result = "max_unanswered"
                        else:
                            await self.cancel_auto_trigger(normalized_session_id)
                            # 只清除自动触发自身的基准，用户发言时间保持原样。
                            self._clear_auto_trigger_baseline(normalized_session_id)
                            auto_result = (
                                await self.setup_auto_trigger_for_session_config(
                                    auto_settings, normalized_session_id
                                )
                            )
                            if auto_result == "created":
                                reset_done = True

            if not reset_done:
                if auto_result == "max_unanswered":
                    return {"ok": False, "reason": "max_unanswered"}
                if auto_result == "untrusted_count":
                    return {"ok": False, "reason": "untrusted_count"}
                if auto_result == "existing":
                    return {"ok": False, "reason": "already_scheduled"}
                return {"ok": False, "reason": "no_timer_config"}

            logger.info(
                f"[主动消息] Web 管理端已重置 {plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的计时器喵。"
            )

        await self._notify_web_console("timer_reset")
        return {"ok": True}

    def _clear_auto_trigger_baseline(self, session_id: str) -> None:
        """重置自动触发基准，但不触碰用户的真实发言时间。

        自动触发判定用「上次发言时间是否早于本轮计时起点」来表达，
        因此只需记录一个计时起点，不能把 last_message_times 清零。
        """
        plugin = self.plugin
        plugin.auto_trigger_baselines[session_id] = time.time()

    async def handle_auto_trigger_callback(
        self,
        session_id: str,
        auto_trigger_minutes: int | float,
        timer_generation: int = 0,
    ) -> None:
        """在异步上下文中处理自动触发回调。

        仅当插件启动后该会话一直无消息、且已达到配置静默时长时才真正触发；
        无论结果如何，最后都会清理相关计时器引用。

        timer_generation 用于识别旧回调。
        """
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            return

        session_id = plugin.session_service.normalize_session_id(session_id)
        try:
            async with plugin.data_lock:
                if session_id not in plugin.auto_trigger_timers:
                    logger.debug(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 的自动触发已被取消，跳过喵。"
                    )
                    return

                current_config = plugin.config_service.get_session_config(session_id)
                if not current_config or not current_config.get(ENABLED_KEY, False):
                    logger.info(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id, current_config)} 的配置已禁用，取消自动触发喵。"
                    )
                    return

                last_message_time = plugin.last_message_times.get(session_id, 0)
                baseline = plugin.auto_trigger_baselines.get(session_id, 0)
                current_time = time.time()
                # 重置过计时器的会话以重置时刻为起点；否则仍以启动时刻为起点。
                origin = baseline or plugin.plugin_start_time

                if last_message_time >= origin or current_time - origin < (
                    auto_trigger_minutes * 60
                ):
                    return

                # 计数缺失但存在历史调度痕迹时无法证明未达上限，保守跳过。
                # 上限为 0 表示不限制，此时必须放行，与初始化 / Reset / GATE 保持一致。
                session_info = plugin.session_data.get(session_id)
                state = resolve_unanswered_state(session_info)
                limit_enabled = is_unanswered_limit_enabled(
                    (current_config.get("schedule_settings", {}) or {}).get(
                        "max_unanswered_times", 3
                    )
                )
                if state.reason == REASON_MISSING_WITH_HISTORY and limit_enabled:
                    # 立即落盘显式标记：后续排期会补写 next_trigger_time 让痕迹失效，
                    # 只有持久标记才能继续拦住该会话。
                    if mark_untrusted(session_info):
                        await plugin.storage_service.save_data()
                    logger.warning(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id, current_config)} 的未回复计数缺失，"
                        "但存在历史调度痕迹，跳过本次自动触发以避免误排期喵。"
                    )
                    return

                # 已有持久化任务时不再重复登记，避免覆盖已排好的发送时间。
                if self.has_related_persisted_task(session_id):
                    logger.debug(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 已有持久化任务，跳过自动触发登记喵。"
                    )
                    return

                schedule_conf = current_config.get("schedule_settings", {})
                (
                    run_date,
                    scheduled_at,
                    min_interval,
                    max_interval,
                    random_interval,
                    _next_trigger_time,
                ) = self.compute_next_trigger(schedule_conf)

                session_payload = plugin.session_data.setdefault(session_id, {})
                # 这里只为确定是全新会话的记录初始化计数。
                if not session_payload:
                    session_payload[UNANSWERED_COUNT_KEY] = 0
                elif resolve_unanswered_state(session_payload).reason == (
                    REASON_MISSING_WITH_HISTORY
                ):
                    mark_untrusted(session_payload)
                session_payload["last_scheduled_at"] = scheduled_at
                session_payload["last_schedule_min_interval_seconds"] = min_interval
                session_payload["last_schedule_max_interval_seconds"] = max_interval
                session_payload["last_schedule_random_interval_seconds"] = (
                    random_interval
                )

                self.add_chat_job(session_id, run_date)

                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id, current_config)} 满足条件，自动触发任务已创建喵！执行时间 (非持久化): {run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵"
                )
        except Exception as e:
            logger.error(f"[主动消息] 自动触发任务创建失败喵: {e}")
        finally:
            # 只清理属于本次回调的计时器：若期间已被重建，代次会变化，
            # 此时直接 pop 会把新计时器的引用删掉，导致重新计时失效。
            if timer_generation and (
                self._current_timer_generation(f"auto:{session_id}") != timer_generation
            ):
                logger.debug(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 的自动触发已被重建，跳过计时器清理喵。"
                )
            else:
                for timer_key in self.iter_related_auto_trigger_keys(session_id):
                    plugin.auto_trigger_timers.pop(timer_key, None)
            # 计时器已从内存移除，卡片必须同步消失，否则会残留过期状态。
            await self._notify_web_console("auto_trigger_finished")

    async def handle_group_silence_callback(
        self, session_id: str, idle_minutes: int | float, timer_generation: int = 0
    ) -> None:
        """在异步上下文中处理群聊沉默回调。

        本回调只负责“计划”主动消息；是否因未回复上限而暂停由 GATE 阶段统一判定。
        timer_generation 用于识别“旧回调”。
        """
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            return

        session_id = plugin.session_service.normalize_session_id(session_id)
        try:
            async with plugin.data_lock:
                # 必须在持锁后重新比对代次：等锁期间可能已经重新计时。
                if timer_generation and (
                    self._current_timer_generation(f"silence:{session_id}")
                    != timer_generation
                ):
                    logger.debug(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 的沉默回调已被重置取代，跳过喵。"
                    )
                    return

                if session_id not in plugin.group_timers:
                    return

                del plugin.group_timers[session_id]
                # 计时器已结束，人为起点随之失效，避免下次重建时沿用旧起点。
                plugin.silence_baselines.pop(session_id, None)

                if session_id not in plugin.session_data:
                    logger.info(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 的会话数据不存在，创建初始会话数据喵。"
                    )
                    plugin.session_data[session_id] = {"unanswered_count": 0}

                current_config = plugin.config_service.get_session_config(session_id)
                if not current_config or not current_config.get(ENABLED_KEY, False):
                    logger.info(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id, current_config)} 的配置已禁用或不存在，跳过主动消息创建喵。"
                    )
                    return

                current_unanswered = plugin.session_data.get(session_id, {}).get(
                    "unanswered_count", 0
                )

            # 与旧实现保持一致：群聊沉默回调只负责“计划”主动消息，
            # 是否因未回复上限而暂停由 GATE 阶段统一判定。
            plugin._track_task(
                asyncio.create_task(
                    plugin.scheduler_service.schedule_next_chat_and_save(
                        session_id, reset_counter=False
                    )
                )
            )
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id, current_config)} 已沉默 {idle_minutes} 分钟，开始计划主动消息喵。(当前未回复次数: {current_unanswered})"
            )
            # 群沉默计时器已从 group_timers 移除，需立即通知前端清掉这张卡片。
            await self._notify_web_console("group_silence_finished")
        except Exception as e:
            logger.error(f"[主动消息] 沉默倒计时回调函数执行失败喵: {e}")

    def cleanup_expired_session_states(self, current_time: float) -> None:
        """清理过期的会话状态，防止内存泄漏。

        超过 5 分钟未活跃的群聊临时状态会被移除。
        """
        plugin = self.plugin
        expired_sessions: list[str] = []
        timeout_seconds = 300

        for session_id, state in plugin.session_temp_state.items():
            last_user_time = state.get("last_user_time", 0)
            if current_time - last_user_time > timeout_seconds:
                expired_sessions.append(session_id)

        for session_id in expired_sessions:
            del plugin.session_temp_state[session_id]
