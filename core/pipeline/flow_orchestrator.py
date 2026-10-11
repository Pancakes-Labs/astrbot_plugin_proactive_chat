"""主动消息流水线编排器。

由旧 core/chat_flow.py 的 check_and_chat 改编而来：骨架不变，
具体步骤交由各 Feature 注册的 Stage 执行。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ...utils.time_utils import is_quiet_time
from ..adapters.astrbot_conversation import add_message_pair, verify_message_persisted
from ..domain.domain_enums import HookPoint, StageResult, TriggerSource
from ..domain.request_context import ProactiveContext
from ..domain.session_key import is_friend_type, is_group_type
from ..features.feature_manifest import build_default_features
from .feature_registry import FeatureRegistry
from .flow_context import build_context
from .pipeline_runner import PipelineRunner


class FlowOrchestrator:
    """组织并执行主动消息流水线。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._runner: PipelineRunner | None = None

    # ------------------------------------------------------------------
    # 流水线装配
    # ------------------------------------------------------------------
    def build_pipeline(self) -> PipelineRunner:
        """构建流水线（装配 Feature 并注入结束行为）。"""
        registry = FeatureRegistry(config=dict(self.plugin.config))
        registry.register_features(build_default_features(self))

        runner = PipelineRunner(registry.hooks)
        runner.set_stop_handler(HookPoint.GATE, self._on_gate_stop)
        runner.set_reschedule_handler(self._on_reschedule)
        runner.set_abort_handler(self._on_abort)
        self._runner = runner
        return runner

    async def _on_gate_stop(self, ctx: ProactiveContext) -> None:
        """GATE 阶段正常终止（未回复上限/无配置）：不重调度。"""
        return None

    async def _on_reschedule(self, ctx: ProactiveContext, reset: bool) -> None:
        """需要重新调度下一次主动消息。"""
        await self.plugin.scheduler_service.schedule_next_chat_and_save(ctx.session_id)

    async def _on_abort(self, ctx: ProactiveContext, error: Exception | None) -> None:
        """阶段异常时的兜底：清理调度痕迹并尝试重调度。

        流程：记录错误 -> 清理残留调度状态 -> 重新排期 -> 上报遥测。
        """
        plugin = self.plugin
        session_id = ctx.session_id
        if error is not None:
            logger.error("[主动消息] check_and_chat 任务发生致命错误喵:")
            logger.error(f"[主动消息] 错误类型喵: {type(error).__name__}")
            logger.error(f"[主动消息] 错误信息喵: {error}")

        try:
            async with plugin.data_lock:
                if plugin.scheduler_service.clear_session_schedule_state(session_id):
                    await plugin.storage_service.save_data()
        except Exception as clean_e:
            logger.debug(f"[主动消息] 清理失败任务数据时出错喵: {clean_e}")

        try:
            logger.info(
                f"[主动消息] 尝试重新调度 {plugin.session_service.get_session_log_str(session_id)} 的主动消息任务喵。"
            )
            await plugin.scheduler_service.schedule_next_chat_and_save(session_id)
        except Exception as se:
            logger.error(f"[主动消息] 在错误处理中重新调度失败喵: {se}")

        if error is not None and plugin.telemetry and plugin.telemetry.enabled:
            plugin._track_task(
                asyncio.create_task(
                    plugin.telemetry.track_error(
                        error, module="core.chat_flow.check_and_chat"
                    )
                )
            )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def run(
        self, session_id: str, trigger: TriggerSource = TriggerSource.SCHEDULE
    ) -> None:
        """执行一次完整的主动消息流程。

        构建上下文并交由流水线执行；发生异常时走 _on_abort 兜底；
        无论成败都会释放手动触发占用状态。
        """
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            logger.debug("[主动消息] 插件正在终止，跳过本次 check_and_chat 喵。")
            return

        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        try:
            session_config = plugin.config_service.get_session_config(
                normalized_session_id
            )
            ctx = build_context(
                normalized_session_id,
                plugin.session_service.build_session_key(normalized_session_id),
                trigger,
                session_config,
                plugin.session_data.get(normalized_session_id, {}).get(
                    "unanswered_count", 0
                ),
            )

            runner = self._runner or self.build_pipeline()
            await runner.run(ctx)
        except Exception as e:
            await self._on_abort(
                build_context(
                    normalized_session_id,
                    plugin.session_service.build_session_key(normalized_session_id),
                    trigger,
                    None,
                ),
                e,
            )
        finally:
            await self.clear_manual_trigger_state(normalized_session_id)

    async def _cleanup_group_schedule_state(self, session_id: str) -> None:
        """群聊成功发送后清理残留调度状态（与旧实现时机一致）。"""
        plugin = self.plugin
        parsed = plugin.session_service.parse_session_id(session_id)
        session_is_group = bool(parsed) and is_group_type(parsed[1])
        if session_is_group:
            async with plugin.data_lock:
                if plugin.scheduler_service.clear_session_schedule_state(session_id):
                    await plugin.storage_service.save_data()

    # ------------------------------------------------------------------
    # 准入与收尾（供 Feature 复用）
    # ------------------------------------------------------------------
    async def is_chat_allowed(self, session_id: str) -> tuple[bool, str]:
        """检查是否允许进行主动聊天，并返回阻断原因。

        阻断原因：session_config_missing / session_disabled / quiet_hours。
        allowed 表示通过校验。
        """
        plugin = self.plugin
        session_config = plugin.config_service.get_session_config(session_id)
        if not session_config:
            return False, "session_config_missing"
        if not session_config.get("enable", False):
            return False, "session_disabled"

        schedule_conf = session_config.get("schedule_settings", {})
        if is_quiet_time(schedule_conf.get("quiet_hours", "1-7"), plugin.timezone):
            return False, "quiet_hours"

        return True, "allowed"

    async def finalize_and_reschedule(
        self,
        session_id: str,
        conv_id: str,
        user_prompt: str,
        assistant_response: str,
        unanswered_count: int,
    ) -> None:
        """主动消息任务完成后的收尾工作。

        步骤：存档对话历史并回读校验 -> 递增未回复计数 -> 私聊安排下一次调度 ->
        群聊清理残留调度状态。
        """
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            logger.info("[主动消息] 插件正在终止，跳过本次主动消息的收尾与重调度喵。")
            return

        if not (assistant_response or "").strip():
            logger.debug("[主动消息] 本次结果无文本内容，跳过对话历史存档喵。")
        else:
            try:
                await add_message_pair(
                    plugin.context.conversation_manager,
                    conv_id,
                    user_prompt,
                    assistant_response,
                )
                if await verify_message_persisted(
                    plugin.context.conversation_manager,
                    session_id,
                    conv_id,
                    assistant_response,
                ):
                    logger.info("[主动消息] 已成功将本次主动消息存档至对话历史喵。")
                else:
                    logger.warning(
                        "[主动消息] 本次主动消息已提交存档，但回读校验未命中末尾记录喵，请检查对话历史持久化是否正常喵。"
                    )
            except Exception as e:
                logger.error(f"[主动消息] 存档对话历史失败喵: {e}")
                logger.warning("[主动消息] 对话存档失败喵，但会继续执行后续步骤喵。")

        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        parsed = plugin.session_service.parse_session_id(normalized_session_id)
        is_private_session = bool(parsed) and is_friend_type(parsed[1])
        session_config = None
        scheduled_job_payload = None

        # 递增未回复计数并落盘；私聊额外计算并写入下一次调度时间。
        async with plugin.data_lock:
            new_unanswered_count = unanswered_count + 1
            plugin.session_data.setdefault(normalized_session_id, {})[
                "unanswered_count"
            ] = new_unanswered_count
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} 的第 {new_unanswered_count} 次主动消息已发送完成，当前未回复次数: {new_unanswered_count} 次喵。"
            )

            if is_private_session:
                session_config = plugin.config_service.get_session_config(
                    normalized_session_id
                )
                if not session_config:
                    return

                schedule_conf = session_config.get("schedule_settings", {})
                (
                    run_date,
                    scheduled_at,
                    min_interval,
                    max_interval,
                    random_interval,
                    next_trigger_time,
                ) = plugin.scheduler_service.compute_next_trigger(schedule_conf)

                session_payload = plugin.session_data.setdefault(
                    normalized_session_id, {}
                )
                session_payload["next_trigger_time"] = next_trigger_time
                session_payload["last_scheduled_at"] = scheduled_at
                session_payload["last_schedule_min_interval_seconds"] = min_interval
                session_payload["last_schedule_max_interval_seconds"] = max_interval
                session_payload["last_schedule_random_interval_seconds"] = (
                    random_interval
                )
                scheduled_job_payload = {
                    "run_date": run_date,
                    "session_config": session_config,
                }

            await plugin.storage_service.save_data()

        if scheduled_job_payload is not None:
            plugin.scheduler_service.add_chat_job(
                normalized_session_id, scheduled_job_payload["run_date"]
            )
            logger.info(
                f"[主动消息] 已为 {plugin.session_service.get_session_log_str(normalized_session_id, scheduled_job_payload['session_config'])} 安排下一次主动消息喵，时间：{scheduled_job_payload['run_date'].strftime('%Y-%m-%d %H:%M:%S')} 喵。"
            )

        # 群聊由沉默倒计时驱动，不依赖持久化调度字段，故在成功发送后清理残留状态。
        await self._cleanup_group_schedule_state(normalized_session_id)

    async def clear_manual_trigger_state(self, session_id: str) -> None:
        """释放指定会话的手动触发占用状态，并向管理端广播任务刷新。"""
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        if normalized_session_id not in plugin.manual_trigger_sessions:
            return

        plugin.manual_trigger_sessions.discard(normalized_session_id)
        if plugin.web_admin_server:
            try:
                await plugin.web_admin_server._broadcast_update("jobs")
            except Exception as e:
                logger.debug(f"[主动消息] 广播手动触发状态更新失败喵: {e}")


__all__ = ["FlowOrchestrator", "StageResult"]
