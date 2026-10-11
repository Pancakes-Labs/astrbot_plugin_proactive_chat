"""DELIVER 阶段默认功能：发送主动消息（含装饰/分段/TTS）。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ...domain.domain_enums import HookPoint, StageResult
from ...pipeline.stage_protocol import BaseStage


class DeliverStage(BaseStage):
    """消息投递阶段。"""

    name = "deliver_stage"
    hook = HookPoint.DELIVER
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        """投递主动消息；发送失败时不存档、不计数，直接重调度。"""
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id
        # extra 承载上游阶段产出的待发送内容。
        extra = getattr(ctx, "extra", {}) or {}

        sent_ok = (
            await plugin.sender_service.send_proactive_message(
                session_id,
                extra.get("response_text", ""),
                event=ctx.event,
                initial_chain=extra.get("result_chain"),
            )
            is True
        )
        if not sent_ok:
            logger.warning(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id)} 的本次主动消息未能送达，已跳过存档与计数并重新调度喵。"
            )
            ctx.abort("send_failed")
            return StageResult.RESCHEDULE
        return StageResult.CONTINUE


class SplitDeliveryFeature:
    """DELIVER 阶段功能（默认发送行为）。"""

    name = "split_delivery_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.DELIVER, stage=DeliverStage(self.orchestrator), priority=10
        )
