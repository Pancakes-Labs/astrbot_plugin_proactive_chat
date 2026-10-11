"""AstrBot 事件监听适配器。

由旧 core/message_events.py 等价搬迁而来；逻辑主体保留，
仅把会话键迁移等重复片段收敛为内部辅助方法。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..domain.session_key import contains_group_marker


class EventHandlers:
    """私聊/群聊/发送后事件的处理逻辑。

    通过构造注入 plugin 实例（容器），复用其服务能力。
    """

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 内部辅助：会话键迁移（集中收敛原重复 4 次的逻辑）
    # ------------------------------------------------------------------
    def _migrate_raw_key(self, session_id: str, normalized_session_id: str) -> bool:
        """把 raw 键的载荷并入规范键，返回是否有变更。"""
        if (
            normalized_session_id == session_id
            or session_id not in self.plugin.session_data
        ):
            return False
        existing_payload = self.plugin.session_data.get(session_id, {})
        self.plugin.session_data.setdefault(normalized_session_id, {}).update(
            existing_payload
        )
        del self.plugin.session_data[session_id]
        return True

    # ------------------------------------------------------------------
    # 内部辅助：用户消息时间记录
    # ------------------------------------------------------------------
    # 说明：内存态与持久化态的消息时间分别写入，二者语义不同，不可合并。
    # 内存态供本进程即时判定，持久化态用于重启后的自动触发恢复。
    def _mark_memory_message_time(
        self, normalized_session_id: str, current_time: float
    ) -> None:
        """即时更新内存态消息时间（供 REVIEW / 自动触发 / 群聊卡片读取）。

        必须在进入 data_lock 之前调用，保证 REVIEW 阶段能立即看到最新时间。
        """
        self.plugin.last_message_times[normalized_session_id] = current_time

    def _mark_persisted_message_time(
        self, normalized_session_id: str, current_time: float
    ) -> bool:
        """在锁内持久化消息时间。

        仅记录“插件启动之后”的时间，避免历史值污染自动触发判定。
        返回是否真正写入，供调用方保留既有日志分支。
        """
        if current_time >= self.plugin.plugin_start_time:
            self.plugin.session_data.setdefault(normalized_session_id, {})[
                "last_message_time"
            ] = current_time
            return True
        return False

    # ------------------------------------------------------------------
    # 私聊
    # ------------------------------------------------------------------
    async def on_friend_message(self, event: AstrMessageEvent) -> None:
        """处理私聊消息：记录时间、取消调度、重置计数并重新排期。"""
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        normalized_session_id = self.plugin.session_service.normalize_session_id(
            session_id
        )

        # 先在锁外记录内存态消息时间，供后续逻辑即时读取。
        current_time = time.time()
        self._mark_memory_message_time(normalized_session_id, current_time)

        async with self.plugin.data_lock:
            # 先迁移旧键，再写入 self_id，避免历史值覆盖当前值。
            self._migrate_raw_key(session_id, normalized_session_id)

            if event.get_self_id():
                self.plugin.session_data.setdefault(normalized_session_id, {})[
                    "self_id"
                ] = event.get_self_id()

            self._mark_persisted_message_time(normalized_session_id, current_time)

        auto_trigger_cancelled = (
            await self.plugin.scheduler_service.cancel_all_related_auto_triggers(
                session_id
            )
        )
        if normalized_session_id != session_id:
            normalized_cancelled = (
                await self.plugin.scheduler_service.cancel_all_related_auto_triggers(
                    normalized_session_id
                )
            )
            auto_trigger_cancelled = auto_trigger_cancelled or normalized_cancelled

        session_config = self.plugin.config_service.get_session_config(
            normalized_session_id
        )
        if (
            auto_trigger_cancelled
            and session_config
            and session_config.get("enable", False)
            and normalized_session_id not in self.plugin.first_message_logged
        ):
            self.plugin.first_message_logged.add(normalized_session_id)
            logger.info(
                f"[主动消息] 已记录 {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的消息时间并取消自动触发喵。"
            )

        session_config = self.plugin.config_service.get_session_config(
            normalized_session_id
        )
        if not session_config or not session_config.get("enable", False):
            logger.debug(
                f"[主动消息] {self.plugin.session_service.get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        cancelled = False
        try:
            self.plugin.scheduler.remove_job(normalized_session_id)
            cancelled = True
        except Exception:
            pass

        if normalized_session_id != session_id:
            try:
                self.plugin.scheduler.remove_job(session_id)
                cancelled = True
            except Exception:
                pass

        self.plugin.scheduler_service.purge_related_jobs(normalized_session_id)

        if cancelled:
            logger.info(
                f"[主动消息] 用户已回复喵，已取消 {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的主动消息任务喵。"
            )

        logger.info(
            f"[主动消息] 重置 {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的未回复计数器为0喵。"
        )
        await self.plugin.scheduler_service.schedule_next_chat_and_save(
            normalized_session_id, reset_counter=True
        )

    # ------------------------------------------------------------------
    # 群聊
    # ------------------------------------------------------------------
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """处理群聊消息：记录活跃时间、重置沉默计时与未回复计数。

        会先剔除 Bot 自身消息，避免把机器人发言误判为用户活跃。
        """
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        normalized_session_id = self.plugin.session_service.normalize_session_id(
            session_id
        )

        async with self.plugin.data_lock:
            self._migrate_raw_key(session_id, normalized_session_id)

        sender_id = None
        try:
            if hasattr(event, "message_obj") and event.message_obj:
                sender = getattr(event.message_obj, "sender", None)
                if sender:
                    sender_id = getattr(sender, "id", None) or getattr(
                        sender, "user_id", None
                    )
            if not sender_id:
                sender_id = getattr(event, "user_id", None) or getattr(
                    event, "sender_id", None
                )
        except Exception as e:
            logger.debug(f"[主动消息] 获取群聊发送者ID失败喵: {e}")

        # 识别并跳过 Bot 自身消息。
        self_id = event.get_self_id() or self.plugin.session_data.get(
            normalized_session_id, {}
        ).get("self_id")
        if self_id and sender_id and str(sender_id) == str(self_id):
            logger.debug(
                f"[主动消息] 检测到 {self.plugin.session_service.get_session_log_str(session_id)} 的 Bot 自身消息，跳过用户逻辑喵。"
            )
            return

        current_time = time.time()
        self.plugin.session_temp_state[normalized_session_id] = {
            "last_user_time": current_time
        }
        logger.debug(
            f"[主动消息] 记录 {self.plugin.session_service.get_session_log_str(session_id)} 的消息时间戳喵: {current_time}"
        )

        self._mark_memory_message_time(normalized_session_id, current_time)

        sender_name = ""
        try:
            sender_name = str(event.get_sender_name() or "")
        except Exception:
            sender_name = ""

        async with self.plugin.data_lock:
            session_payload = self.plugin.session_data.setdefault(
                normalized_session_id, {}
            )
            if event.get_self_id():
                session_payload["self_id"] = event.get_self_id()
            if sender_id:
                session_payload["last_sender_id"] = str(sender_id)
            if sender_name:
                session_payload["last_sender_name"] = sender_name

            if self._mark_persisted_message_time(normalized_session_id, current_time):
                logger.debug(
                    f"[主动消息] 已记录插件启动后 {self.plugin.session_service.get_session_log_str(session_id)} 的消息时间喵 -> {current_time}"
                )
            else:
                logger.debug(
                    f"[主动消息] 忽略插件启动前 {self.plugin.session_service.get_session_log_str(session_id)} 的旧消息用于自动主动消息任务喵 -> {current_time}"
                )

        auto_trigger_cancelled = (
            await self.plugin.scheduler_service.cancel_all_related_auto_triggers(
                session_id
            )
        )
        if normalized_session_id != session_id:
            normalized_cancelled = (
                await self.plugin.scheduler_service.cancel_all_related_auto_triggers(
                    normalized_session_id
                )
            )
            auto_trigger_cancelled = auto_trigger_cancelled or normalized_cancelled

        session_config = self.plugin.config_service.get_session_config(
            normalized_session_id
        )

        if (
            auto_trigger_cancelled
            and session_config
            and session_config.get("enable", False)
            and normalized_session_id not in self.plugin.first_message_logged
        ):
            self.plugin.first_message_logged.add(normalized_session_id)
            logger.info(
                f"[主动消息] 已记录 {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的消息时间并取消自动触发喵。"
            )

        if not session_config or not session_config.get("enable", False):
            logger.debug(
                f"[主动消息] {self.plugin.session_service.get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        had_scheduled_task = False
        if self.plugin.scheduler.get_job(normalized_session_id):
            had_scheduled_task = True
        if normalized_session_id != session_id and self.plugin.scheduler.get_job(
            session_id
        ):
            had_scheduled_task = True
        if (
            not had_scheduled_task
            and normalized_session_id in self.plugin.session_data
            and self.plugin.scheduler_service.is_persisted_task_still_valid(
                normalized_session_id,
                self.plugin.session_data.get(normalized_session_id),
                current_time=current_time,
            )
        ):
            had_scheduled_task = True

        cancelled = False
        try:
            self.plugin.scheduler.remove_job(normalized_session_id)
            cancelled = True
        except Exception:
            pass

        if normalized_session_id != session_id:
            try:
                self.plugin.scheduler.remove_job(session_id)
                cancelled = True
            except Exception:
                pass

        self.plugin.scheduler_service.purge_related_jobs(normalized_session_id)

        if cancelled:
            logger.info(
                f"[主动消息] 群聊活跃喵，已取消 {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的主动消息任务喵。"
            )
        elif had_scheduled_task:
            logger.info(
                f"[主动消息] 群聊活跃喵，{self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 未找到可取消的主动消息任务（可能已被提前清理）喵。"
            )

        await self.plugin.scheduler_service.reset_group_silence_timer(
            normalized_session_id
        )

        async with self.plugin.data_lock:
            changed = False
            if normalized_session_id in self.plugin.session_data:
                current_unanswered = self.plugin.session_data[
                    normalized_session_id
                ].get("unanswered_count", 0)
                self.plugin.session_data[normalized_session_id]["unanswered_count"] = 0
                changed = True
                if current_unanswered > 0:
                    logger.debug(
                        f"[主动消息] {self.plugin.session_service.get_session_log_str(normalized_session_id, session_config)} 的用户已回复， 未回复计数器已重置喵。"
                    )

                if contains_group_marker(normalized_session_id):
                    changed = (
                        self.plugin.scheduler_service.clear_session_schedule_state(
                            normalized_session_id
                        )
                        or changed
                    )

            if changed:
                await self.plugin.storage_service.save_data()

    # ------------------------------------------------------------------
    # 发送后
    # ------------------------------------------------------------------
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后钩子：仅对群聊做任务取消与状态清理。

        Bot 主动发言后应取消待触发的主动消息，并周期性清理过期临时状态。
        """
        session_id = event.unified_msg_origin
        normalized_session_id = self.plugin.session_service.normalize_session_id(
            session_id
        )

        if not contains_group_marker(normalized_session_id):
            return

        try:
            self.plugin.scheduler.remove_job(normalized_session_id)
            if normalized_session_id != session_id:
                self.plugin.scheduler.remove_job(session_id)
            logger.debug(
                f"[主动消息] Bot已发言，已取消 {self.plugin.session_service.get_session_log_str(normalized_session_id)} 的主动消息任务喵。"
            )
        except Exception as e:
            logger.debug(
                f"[主动消息] {self.plugin.session_service.get_session_log_str(normalized_session_id)} 没有待取消的调度任务喵: {e}"
            )

        self.plugin.scheduler_service.purge_related_jobs(normalized_session_id)

        async with self.plugin.data_lock:
            changed = False
            if self._migrate_raw_key(session_id, normalized_session_id):
                changed = True

            if self.plugin.scheduler_service.clear_session_schedule_state(
                normalized_session_id
            ):
                changed = True

            if changed:
                await self.plugin.storage_service.save_data()

        current_time = time.time()
        self.plugin._cleanup_counter += 1

        if self.plugin._cleanup_counter % 10 == 0:
            self.plugin.scheduler_service.cleanup_expired_session_states(current_time)

        try:
            await self.plugin.scheduler_service.reset_group_silence_timer(
                normalized_session_id
            )
            if normalized_session_id in self.plugin.session_temp_state:
                del self.plugin.session_temp_state[normalized_session_id]
        except Exception as e:
            logger.error(
                f"[主动消息] {self.plugin.session_service.get_session_log_str(session_id)} 的 after_message_sent 处理异常喵: {e}"
            )
