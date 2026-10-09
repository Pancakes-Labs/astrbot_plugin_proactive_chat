"""GATE 阶段默认功能：启用/免打扰/未回复上限校验。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult
from ..pipeline.stage_protocol import BaseStage


class GatingStage(BaseStage):
    """准入校验阶段。"""

    name = "gating_stage"
    hook = HookPoint.GATE
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        """执行准入校验：免打扰 / 会话禁用 / 未回复上限。

        不满足条件时按原因记录日志并中止流水线（RESCHEDULE 或 STOP）。
        """
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id

        is_allowed, block_reason = await self.orchestrator.is_chat_allowed(session_id)
        if not is_allowed:
            # 免打扰、会话禁用、配置缺失等应重新调度；其余原因统一提示。
            if block_reason == "quiet_hours":
                logger.info("[主动消息] 当前为免打扰时段，跳过并重新调度喵。")
            elif block_reason == "session_disabled":
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 已被禁用，跳过并重新调度喵。"
                )
            elif block_reason == "session_config_missing":
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 未命中有效会话配置，跳过并重新调度喵。"
                )
            else:
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 当前不满足触发条件（原因: {block_reason}），跳过并重新调度喵。"
                )
            ctx.abort(block_reason)
            return StageResult.RESCHEDULE

        session_config = plugin.config_service.get_session_config(session_id)
        if not session_config:
            # 配置缺失：终止本次流程，不再重调度。
            ctx.abort("session_config_missing")
            return StageResult.STOP

        schedule_conf = session_config.get("schedule_settings", {})
        # 在锁内读取未回复计数并与上限比较。
        async with plugin.data_lock:
            unanswered_count = plugin.session_data.get(session_id, {}).get(
                "unanswered_count", 0
            )
            max_unanswered = schedule_conf.get("max_unanswered_times", 3)
            if max_unanswered > 0 and unanswered_count >= max_unanswered:
                logger.info(
                    f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} 的未回复次数 ({unanswered_count}) 已达到上限 ({max_unanswered})，暂停主动消息喵。"
                )
                ctx.abort("max_unanswered")
                return StageResult.STOP

        ctx.unanswered_count = unanswered_count

        logger.info(
            f"[主动消息] 开始生成第 {unanswered_count + 1} 次主动消息喵，当前未回复次数: {unanswered_count} 次喵。"
        )
        # 遥测启用时上报“主动任务已开始”。
        if plugin.telemetry and plugin.telemetry.enabled:
            plugin._track_task(
                asyncio.create_task(
                    plugin.telemetry.track_feature(
                        "proactive_task_started",
                        {
                            "session_type": session_config.get(
                                "_session_type", "unknown"
                            ),
                            "unanswered_count": unanswered_count,
                        },
                    )
                )
            )
        return StageResult.CONTINUE


class GatingFeature:
    """GATE 阶段功能。"""

    name = "gating_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.GATE, stage=GatingStage(self.orchestrator), priority=10
        )
