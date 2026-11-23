"""主动消息流水线编排器。

由旧 core/chat_flow.py 的 check_and_chat 改编而来：骨架不变，
具体步骤交由各 Feature 注册的 Stage 执行。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ...utils.time_utils import is_quiet_time
from ..adapters.astrbot_conversation import (
    add_message_pair,
    count_history_records,
    verify_message_persisted,
    verify_pair_persisted,
)
from ..domain.config_keys import ENABLED_KEY, HISTORY_ARCHIVE_MODE_KEY
from ..domain.domain_enums import HookPoint, StageResult, TriggerSource
from ..domain.history_marker import (
    ARCHIVE_MODE_SEMANTIC_EVENT,
    BARE_MARKER,
    build_proactive_history_text,
    contains_proactive_marker,
    normalize_archive_mode,
)
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
    def build_features(self) -> list:
        """构造默认 Feature 列表。

        仅用于读取 Feature 声明的配置，不执行任何 Stage；
        配置装配服务与流水线构建共用同一份清单，避免顺序不一致。
        """
        return build_default_features(self)

    def build_pipeline(self) -> PipelineRunner:
        """构建流水线（装配 Feature 并注入结束行为）。"""
        registry = FeatureRegistry(config=dict(self.plugin.config))
        registry.register_features(self.build_features())

        runner = PipelineRunner(registry.hooks)
        runner.set_stop_handler(HookPoint.GATE, self._on_gate_stop)
        runner.set_reschedule_handler(self._on_reschedule)
        runner.set_abort_handler(self._on_abort)
        self._runner = runner
        return runner

    async def _on_gate_stop(self, ctx: ProactiveContext) -> None:
        """GATE 阶段正常终止（未回复上限/无配置）：不重调度。"""
        return None

    def _cancelled_marker(self) -> set[str] | None:
        """返回取消标记集合；属性缺失或类型异常时返回 None。"""
        marker = getattr(self.plugin, "cancelled_trigger_sessions", None)
        return marker if isinstance(marker, set) else None

    def _trigger_cancelled(self, ctx: ProactiveContext) -> bool:
        """只读判断本轮手动触发是否已被用户取消。

        仅手动触发的运行参与：排期触发的并发运行不能因为同一会话
        存在取消标记就放弃自己的重新排期。
        """
        if ctx.trigger is not TriggerSource.MANUAL:
            return False
        marker = self._cancelled_marker()
        if not marker:
            return False
        session_id = self.plugin.session_service.normalize_session_id(ctx.session_id)
        return session_id in marker

    def _consume_trigger_cancel(self, ctx: ProactiveContext) -> bool:
        """读取并消费本轮取消标记，返回是否命中。

        调用方必须持有 data_lock：cancel_job 在锁内写入标记，
        只有同为锁内的消费才能保证读到的是最新意图。
        """
        if not self._trigger_cancelled(ctx):
            return False
        marker = self._cancelled_marker()
        if marker is not None:
            marker.discard(
                self.plugin.session_service.normalize_session_id(ctx.session_id)
            )
        return True

    async def _on_reschedule(self, ctx: ProactiveContext, reset: bool) -> None:
        """需要重新调度下一次主动消息。"""
        plugin = self.plugin
        cancelled = [False]

        def cancel_check() -> bool:
            # 由 schedule_next_chat_and_save 在持锁时调用：
            # 只有与任务登记同处一个临界区，取消检查才不会被并发取消绕过。
            cancelled[0] = self._consume_trigger_cancel(ctx)
            return cancelled[0]

        await plugin.scheduler_service.schedule_next_chat_and_save(
            ctx.session_id,
            cancel_check=cancel_check if ctx.trigger is TriggerSource.MANUAL else None,
        )
        if cancelled[0]:
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(ctx.session_id)} 的主动消息已被取消，跳过重新排期喵。"
            )

    async def _on_abort(self, ctx: ProactiveContext, error: Exception | None) -> None:
        """阶段异常时的兜底：清理调度痕迹并尝试重调度。

        流程：记录错误 -> 消费取消标记并清理残留调度状态 -> 重新排期 -> 上报遥测。
        已取消的会话不重新排期，否则错误兜底会把用户取消的任务重新登记。
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

        cancelled = [False]

        def cancel_check() -> bool:
            # 锁内消费取消标记：与任务登记同一临界区，避免并发取消被漏判。
            cancelled[0] = self._consume_trigger_cancel(ctx)
            return cancelled[0]

        try:
            logger.info(
                f"[主动消息] 尝试重新调度 {plugin.session_service.get_session_log_str(session_id)} 的主动消息任务喵。"
            )
            await plugin.scheduler_service.schedule_next_chat_and_save(
                session_id,
                cancel_check=(
                    cancel_check if ctx.trigger is TriggerSource.MANUAL else None
                ),
            )
        except Exception as se:
            logger.error(f"[主动消息] 在错误处理中重新调度失败喵: {se}")

        if cancelled[0]:
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 的主动消息已被取消，错误兜底跳过重新排期喵。"
            )

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
            # 只有手动触发才持有该占用。
            if trigger is TriggerSource.MANUAL:
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
        if not session_config.get(ENABLED_KEY, False):
            return False, "session_disabled"

        schedule_conf = session_config.get("schedule_settings", {})
        # 免打扰开关缺省视为开启，保持旧配置行为不变。
        if schedule_conf.get("quiet_hours_enabled", True) and is_quiet_time(
            schedule_conf.get("quiet_hours", "1-7"), plugin.timezone
        ):
            return False, "quiet_hours"

        return True, "allowed"

    async def finalize_and_reschedule(self, ctx: ProactiveContext) -> None:
        """主动消息任务完成后的收尾工作。

        步骤：存档对话历史并回读校验 -> 递增未回复计数 -> 私聊安排下一次调度 ->
        群聊清理残留调度状态。

        待存档内容统一取自 ctx.outcome 与 ctx.prompts，不再接收散装参数。
        """
        plugin = self.plugin
        if getattr(plugin, "_terminating", False):
            logger.info("[主动消息] 插件正在终止，跳过本次主动消息的收尾与重调度喵。")
            return

        session_id = ctx.session_id
        conv_id = ctx.conversation_id
        outcome = ctx.outcome
        # 本轮真正发给模型的完整提示词
        full_prompt = (
            outcome.final_user_prompt if outcome and outcome.final_user_prompt else ""
        ) or ctx.prompts.merge_text()
        # 序号在锁内基于会话实际计数分配
        unanswered_index = await self._allocate_archive_index(ctx)
        archive_mode, archive_session_type = self._resolve_archive_context(ctx)
        user_prompt = self._build_history_user_text(
            ctx,
            archive_mode,
            archive_session_type,
            full_prompt,
            unanswered_index,
        )
        assistant_response = outcome.text if outcome else ""
        # 优先用「真实送达」的内容归档：部分批次失败时不能把原文整体存档，
        # 否则用户没收到的话也会出现在下一轮提示词里。
        delivered = self._delivered_text_of(ctx)
        if delivered:
            assistant_response = delivered
        # 纯媒体轮次不能跳过存档：跳过会让未回复计数与历史脱节，
        # 模型下一轮也完全不知道自己发过图。改为写入媒体占位文本。
        if outcome is not None:
            assistant_response = self._merge_media_archive_text(
                ctx, assistant_response, outcome
            )

        if not (assistant_response or "").strip():
            logger.debug("[主动消息] 本次结果无文本内容，跳过对话历史存档喵。")
        else:
            try:
                await self._save_history_pair(
                    session_id,
                    conv_id,
                    user_prompt,
                    assistant_response,
                    full_prompt,
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

        # 计数已在上面持锁分配并写入，这里只做调度相关的收尾。
        async with plugin.data_lock:
            # 取消标记在锁内读取并消费。
            cancelled = self._consume_trigger_cancel(ctx)
            if cancelled:
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} "
                    "的主动消息在触发生成期间被取消，跳过下一次排期喵。"
                )
            new_unanswered_count = unanswered_index
            # 记录本轮实际发出的模板，供下一轮判重。
            self._remember_used_template(normalized_session_id, ctx)
            logger.info(
                f"[主动消息] {plugin.session_service.get_session_log_str(normalized_session_id)} 的第 {new_unanswered_count} 次主动消息已发送完成，当前未回复次数: {new_unanswered_count} 次喵。"
            )

            if is_private_session and not cancelled:
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
                    "scheduled_at": scheduled_at,
                    "session_config": session_config,
                }

            # 放在锁内完成。
            if scheduled_job_payload is not None:
                plugin.scheduler_service.add_chat_job(
                    normalized_session_id,
                    scheduled_job_payload["run_date"],
                    scheduled_at=scheduled_job_payload["scheduled_at"],
                )

            await plugin.storage_service.save_data()

        if scheduled_job_payload is not None:
            logger.info(
                f"[主动消息] 已为 {plugin.session_service.get_session_log_str(normalized_session_id, scheduled_job_payload['session_config'])} 安排下一次主动消息喵，时间：{scheduled_job_payload['run_date'].strftime('%Y-%m-%d %H:%M:%S')} 喵。"
            )

        # 群聊由沉默倒计时驱动，不依赖持久化调度字段，故在成功发送后清理残留状态。
        await self._cleanup_group_schedule_state(normalized_session_id)

    async def _allocate_archive_index(self, ctx: ProactiveContext) -> int:
        """在锁内基于会话实际计数分配本轮序号。"""
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(
            ctx.session_id
        )
        async with plugin.data_lock:
            payload = plugin.session_data.setdefault(normalized_session_id, {})
            try:
                current = int(payload.get("unanswered_count", 0) or 0)
            except (TypeError, ValueError):
                current = 0
            index = max(current, 0) + 1
            payload["unanswered_count"] = index
            try:
                await plugin.storage_service.save_data()
            except Exception as e:  # noqa: BLE001 - 落盘失败不应阻断存档
                logger.warning(
                    f"[主动消息] 分配未回复序号后落盘失败，将在收尾阶段重试喵: {e}"
                )
        return index

    def _resolve_archive_context(self, ctx: ProactiveContext) -> tuple[str, str]:
        """读取存档形态与会话类型，返回 (mode, session_type)。

        会话配置缺失时按会话 ID 反推类型。
        """
        mode = ""
        session_type = ""
        config = ctx.config
        if config is not None:
            mode = str(config.context_settings.get(HISTORY_ARCHIVE_MODE_KEY, "") or "")
            session_type = str(config.session_type or "")

        if not session_type:
            parsed = self.plugin.session_service.parse_session_id(ctx.session_id)
            if parsed:
                if is_group_type(parsed[1]):
                    session_type = "group"
                elif is_friend_type(parsed[1]):
                    session_type = "friend"

        return normalize_archive_mode(mode), session_type

    def _build_history_user_text(
        self,
        ctx: ProactiveContext,
        mode: str,
        session_type: str,
        full_prompt: str,
        unanswered_index: int,
    ) -> str:
        """生成写入对话历史的 user 侧文本。

        存档与请求已经解耦：这里返回什么都不会影响本轮已发出的内容，
        只影响下一轮及以后回灌给模型的历史。

        Args:
            ctx: 本轮执行上下文。
            mode: 已归一化的存档形态。
            session_type: friend / group。
            full_prompt: 本轮实际发送的完整提示词。
            unanswered_index: 本轮在沉默链里的序号。

        Returns:
            待存档文本；清空形态返回空串。
        """
        # 只有事件标记需要判断段首：凭计数判断不可靠，清空历史或切换形态后
        # 计数仍大于 0 而标记已不存在，模型会看到没有解释的第 N 次。
        has_prior_marker = True
        if mode == ARCHIVE_MODE_SEMANTIC_EVENT and unanswered_index > 1:
            has_prior_marker = contains_proactive_marker(ctx.history)
            if not has_prior_marker:
                logger.info(
                    "[主动消息] 历史中没有既有的主动消息标记，本轮改按段首写入喵。"
                )

        text = build_proactive_history_text(
            mode,
            full_text=full_prompt,
            unanswered_index=unanswered_index,
            session_type=session_type,
            has_prior_marker=has_prior_marker,
        )
        # 打印存档体积。形态名取归一化后的值。
        logger.info(
            f"[主动消息] 历史存档形态使用 {mode}，"
            f"本次写入 {len(text)} 字（原完整提示词为 {len(full_prompt)} 字）喵。"
        )
        return text

    async def _save_history_pair(
        self,
        session_id: str,
        conv_id: str,
        user_prompt: str,
        assistant_response: str,
        fallback_prompt: str,
    ) -> None:
        """存档一对消息，并在被拒收时按短标记 -> 完整提示词两级回退。"""
        manager = self.plugin.context.conversation_manager
        # 写入前记录基线：主动消息的历史内容高度重复，只有新增记录
        # 才能证明本轮真的落盘了，末尾文本匹配会把上一轮的相同记录误判成本轮。
        baseline = await count_history_records(manager, session_id, conv_id)
        try:
            await add_message_pair(manager, conv_id, user_prompt, assistant_response)
            return
        except Exception as e:
            # 异常对象在 except 块结束后会被 Python 删除，在这里用完。
            error_text = f"{type(e).__name__}: {e}"

        # 部分提交（user 已写入、assistant 失败）时不能整对重发，否则会重复。
        user_ok, assistant_ok = await verify_pair_persisted(
            manager,
            session_id,
            conv_id,
            user_prompt,
            assistant_response,
            since=baseline,
        )
        if user_ok and assistant_ok:
            logger.info("[主动消息] 存档虽抛异常，但回读确认两侧均已落盘喵。")
            return

        last_error: Exception = RuntimeError("存档回退序列为空")
        for retry_text in self._archive_fallback_texts(user_prompt, fallback_prompt):
            logger.warning(
                f"[主动消息] 存档被拒绝（{error_text}），"
                f"改用「{retry_text[:20]}」重试一次喵。"
            )
            try:
                await add_message_pair(manager, conv_id, retry_text, assistant_response)
                return
            except Exception as retry_error:  # noqa: BLE001 - 逐级回退
                last_error = retry_error

        raise last_error

    @staticmethod
    def _delivered_text_of(ctx: ProactiveContext) -> str:
        """读取 DELIVER 阶段回填的真实送达文本；无报告时返回空串。"""
        report = getattr(ctx, "delivery", None)
        delivered = getattr(report, "delivered_text", "") if report else ""
        return str(delivered or "").strip()

    @staticmethod
    def _merge_media_archive_text(
        ctx: ProactiveContext, assistant_response: str, outcome: Any
    ) -> str:
        """把真正送达的媒体摘要合并进归档文本。

        只以 DELIVER 回填的 media_summary 为准：媒体全部发送失败、
        被策略剥离或被钩子清空时该字段为空，此时不生成任何媒体记录，
        避免历史里出现用户从未收到的内容。
        """
        report = getattr(ctx, "delivery", None)
        if report is not None and not getattr(report, "sent", False):
            # 本轮实际未送达任何内容（例如装饰钩子清空），保持静默跳过。
            return assistant_response
        summary = str(getattr(outcome, "media_summary", "") or "").strip()
        if not summary:
            return assistant_response

        media_text = f"[主动发送媒体：{summary}]"
        if not assistant_response.strip():
            return media_text
        return f"{assistant_response}\n{media_text}"

    @staticmethod
    def _archive_fallback_texts(user_prompt: str, fallback_prompt: str) -> list[str]:
        """构造回退文案序列：跳过与原文相同的项，保持短标记优先。"""
        candidates = [BARE_MARKER, fallback_prompt]
        texts: list[str] = []
        for item in candidates:
            text = str(item or "").strip()
            if text and text != user_prompt and text not in texts:
                texts.append(text)
        return texts or ["主动消息"]

    def _remember_used_template(self, session_id: str, ctx: ProactiveContext) -> None:
        """记录本轮实际发出的提示词模板，供下一轮回避判重。

        元数据取自 PROMPT 阶段写入的 PromptBlock：template_id 可能为空，
        因此同时带上 name 与 raw_text，让指纹函数能退化为内容摘要。
        """
        plugin = self.plugin
        service = getattr(plugin, "prompt_preset_service", None)
        if service is None:
            return
        template = self._used_template_of(ctx)
        if not template:
            return
        try:
            service.remember_template(session_id, template)
        except Exception as e:  # noqa: BLE001 - 记录失败不应影响收尾
            logger.warning(f"[主动消息] 记录本轮提示词模板失败喵: {e}")

    @staticmethod
    def _used_template_of(ctx: ProactiveContext) -> dict[str, Any]:
        """从提示词块里取出本轮用户模板的身份信息；未找到时返回空字典。"""
        for block in ctx.prompts.blocks:
            metadata = block.metadata or {}
            if metadata.get("layer") != "user":
                continue
            return {
                "id": str(metadata.get("template_id") or ""),
                "name": str(metadata.get("template_name") or ""),
                "text": str(metadata.get("raw_text") or ""),
            }
        return {}

    async def clear_manual_trigger_state(self, session_id: str) -> None:
        """释放指定会话的手动触发占用状态，并向管理端广播任务刷新。

        只应由持有该占用的手动触发执行调用。
        """
        plugin = self.plugin
        normalized_session_id = plugin.session_service.normalize_session_id(session_id)
        if normalized_session_id not in plugin.manual_trigger_sessions:
            # 未持有占用：不碰取消标记，避免误清别处仍在进行的运行。
            return

        cancelled = getattr(plugin, "cancelled_trigger_sessions", None)
        if cancelled is not None:
            cancelled.discard(normalized_session_id)

        plugin.manual_trigger_sessions.discard(normalized_session_id)
        if plugin.web_admin_server:
            try:
                await plugin.web_admin_server._broadcast_update("jobs")
            except Exception as e:
                logger.debug(f"[主动消息] 广播手动触发状态更新失败喵: {e}")


__all__ = ["FlowOrchestrator", "StageResult"]
