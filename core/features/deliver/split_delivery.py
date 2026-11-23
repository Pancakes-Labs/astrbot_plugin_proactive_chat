"""DELIVER 阶段默认功能：发送主动消息（含装饰/分段/TTS）。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ...domain.config_keys import RICH_MEDIA_SETTINGS_KEY, SESSION_DEFAULTS_KEY
from ...domain.delivery_report import DeliveryReport
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
        outcome = ctx.outcome
        if outcome is None or not outcome.has_content:
            logger.warning("[主动消息] 投递阶段缺少生成结果，放弃本次主动消息喵。")
            ctx.abort("missing_outcome")
            return StageResult.RESCHEDULE

        report = DeliveryReport()
        ctx.delivery = report
        sent_ok = (
            await plugin.sender_service.send_proactive_message(
                session_id,
                outcome.text,
                event=ctx.event,
                initial_chain=outcome.chain,
                report=report,
                outcome=outcome,
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
    CONFIG_SECTION = SESSION_DEFAULTS_KEY
    RICH_MEDIA_PATH = f"{SESSION_DEFAULTS_KEY}.{RICH_MEDIA_SETTINGS_KEY}"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.DELIVER, stage=DeliverStage(self.orchestrator), priority=10
        )

    def config_schema(self) -> dict[str, Any] | None:
        """富媒体分组字段已由静态 schema 声明，避免两处漂移。"""
        return None

    def default_config(self) -> dict[str, Any]:
        """为旧配置补齐富媒体默认值，缺项时按乐观放行处理。"""
        return {
            self.RICH_MEDIA_PATH: {
                "enabled": True,
                "media_grouping": "follow_text",
                "max_media_per_batch": 4,
                "degrade_on_failure": True,
                "strip_interactive_components": True,
                "unknown_component_policy": "drop",
                "restrict_local_media": True,
                "media_timeout_seconds": 30,
                "media_interval_seconds": 1,
                "tts_order": "voice_first",
            }
        }
