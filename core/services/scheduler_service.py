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

from ..domain.session_key import is_friend_type


class SchedulerService:
    """定时任务、自动触发与沉默计时服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 自动触发
    # ------------------------------------------------------------------
    async def setup_auto_trigger(self, session_id: str, silent: bool = False) -> None:
        """为指定会话设置自动主动消息触发器。"""
        plugin = self.plugin
        session_id = plugin.session_service.normalize_session_id(session_id)
        session_config = plugin.config_service.get_session_config(session_id)
        if not session_config:
            return

        auto_trigger_settings = session_config.get("auto_trigger_settings", {})
        if not auto_trigger_settings.get("enable_auto_trigger", False):
            logger.debug(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} 未启用自动主动消息功能喵。"
            )
            return

        auto_trigger_minutes = auto_trigger_settings.get(
            "auto_trigger_after_minutes", 5
        )
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

        def _auto_trigger_callback(captured_session_id=session_id):
            plugin._track_task(
                asyncio.create_task(
                    self.handle_auto_trigger_callback(
                        captured_session_id, auto_trigger_minutes
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
        if not session_config or not session_config.get("enable", False):
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
        min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
        max_interval = max(
            min_interval,
            int(schedule_conf.get("max_interval_minutes", 900)) * 60,
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
        """将配置中的会话标识解析为完整 UMO。"""
        plugin = self.plugin
        parsed = plugin.session_service.parse_session_id(session_id)
        if parsed:
            return session_id

        session_type = session_config.get("_session_type", "friend")
        msg_type = "FriendMessage" if session_type == "friend" else "GroupMessage"
        return plugin.session_service.resolve_full_umo(str(session_id), msg_type)

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

        # 私聊会话：仅在 friend_settings 启用时处理。
        friend_settings = plugin.config.get("friend_settings", {})
        if friend_settings.get("enable", False):
            for session_id in friend_settings.get("session_list", []):
                result = await self.setup_auto_trigger_for_session_config(
                    friend_settings, session_id
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

        # 群聊会话：仅在 group_settings 启用时处理。
        group_settings = plugin.config.get("group_settings", {})
        if group_settings.get("enable", False):
            for session_id in group_settings.get("session_list", []):
                result = await self.setup_auto_trigger_for_session_config(
                    group_settings, session_id
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

        has_auto_trigger_config = False
        if friend_settings.get("auto_trigger_settings", {}).get(
            "enable_auto_trigger", False
        ):
            has_auto_trigger_config = True
        if group_settings.get("auto_trigger_settings", {}).get(
            "enable_auto_trigger", False
        ):
            has_auto_trigger_config = True

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
                f"已达未回复上限 {skipped_max_unanswered}）"
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
        if not session_config or not session_config.get("enable", False):
            return "invalid"

        resolved_session_id = self.resolve_session_id_for_config(
            session_id, session_config
        )

        auto_trigger_settings = session_config.get("auto_trigger_settings", {})
        if not auto_trigger_settings.get("enable_auto_trigger", False):
            logger.debug(
                f"[主动消息] {plugin.session_service.get_session_log_str(resolved_session_id)} 未启用自动主动消息功能喵。"
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
        unanswered_count = plugin.session_data.get(resolved_session_id, {}).get(
            "unanswered_count", 0
        )
        if max_unanswered > 0 and unanswered_count >= max_unanswered:
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(resolved_session_id, session_config)} 的未回复次数 ({unanswered_count}) "
                f"已达到上限 ({max_unanswered})，跳过初始化自动触发器设置喵。"
            )
            return "max_unanswered"

        logger.debug(
            f"[主动消息] 正在为 {plugin.session_service.get_session_log_str(resolved_session_id)} 设置自动触发器喵。"
        )
        auto_trigger_minutes = auto_trigger_settings.get(
            "auto_trigger_after_minutes", 5
        )
        logger.info(
            f"[主动消息] 已为 {plugin.session_service.get_session_log_str(resolved_session_id)} 设置自动触发器喵，"
            f"将在 {auto_trigger_minutes} 分钟后检查是否需要自动触发喵。"
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
            if not session_config or not session_config.get("enable", False):
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
                plugin.session_data.setdefault(normalized_session_id, {})[
                    "unanswered_count"
                ] = 0

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
    async def reset_group_silence_timer(self, session_id: str) -> None:
        """重置指定群聊的沉默倒计时。

        先取消旧计时器（含原始键与规范键），再按配置的静默分钟数重新计时。
        """
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        session_config = plugin.config_service.get_session_config(normalized_session_id)
        if not session_config or not session_config.get("enable", False):
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

        idle_minutes = session_config.get("group_idle_trigger_minutes", 10)

        def _schedule_callback(captured_session_id=normalized_session_id):
            plugin._track_task(
                asyncio.create_task(
                    self.handle_group_silence_callback(
                        captured_session_id, idle_minutes
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

    async def handle_auto_trigger_callback(
        self, session_id: str, auto_trigger_minutes: int | float
    ) -> None:
        """在异步上下文中处理自动触发回调。

        仅当插件启动后该会话一直无消息、且已达到配置静默时长时才真正触发；
        无论结果如何，最后都会清理相关计时器引用。
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
                if not current_config or not current_config.get("enable", False):
                    logger.info(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id, current_config)} 的配置已禁用，取消自动触发喵。"
                    )
                    return

                last_message_time = plugin.last_message_times.get(session_id, 0)
                current_time = time.time()
                time_since_plugin_start = current_time - plugin.plugin_start_time

                if last_message_time != 0 or time_since_plugin_start < (
                    auto_trigger_minutes * 60
                ):
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
            for timer_key in self.iter_related_auto_trigger_keys(session_id):
                plugin.auto_trigger_timers.pop(timer_key, None)

    async def handle_group_silence_callback(
        self, session_id: str, idle_minutes: int | float
    ) -> None:
        """在异步上下文中处理群聊沉默回调。

        本回调只负责“计划”主动消息；是否因未回复上限而暂停由 GATE 阶段统一判定。
        """
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            return

        session_id = plugin.session_service.normalize_session_id(session_id)
        try:
            async with plugin.data_lock:
                if session_id not in plugin.group_timers:
                    return

                del plugin.group_timers[session_id]

                if session_id not in plugin.session_data:
                    logger.info(
                        f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 的会话数据不存在，创建初始会话数据喵。"
                    )
                    plugin.session_data[session_id] = {"unanswered_count": 0}

                current_config = plugin.config_service.get_session_config(session_id)
                if not current_config or not current_config.get("enable", False):
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
