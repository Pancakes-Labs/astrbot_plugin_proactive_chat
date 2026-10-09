"""REVIEW 阶段默认功能：生成期间新消息检测与终止保护。

对应旧 check_and_chat 中「检查生成期间是否有新消息」与
「发送前再次确认终止状态」两段逻辑。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult
from ..pipeline.stage_protocol import BaseStage


class ReviewStage(BaseStage):
    """生成结果复核阶段。"""

    name = "review_stage"
    hook = HookPoint.REVIEW
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id

        current_state = {
            "last_message_time": plugin.last_message_times.get(session_id, 0),
            "unanswered_count": plugin.session_data.get(session_id, {}).get(
                "unanswered_count", 0
            ),
        }
        start_state = ctx.start_state or {}

        has_new_message = current_state["last_message_time"] > start_state.get(
            "last_message_time", 0
        ) or current_state["unanswered_count"] < start_state.get("unanswered_count", 0)
        if has_new_message:
            logger.info(
                "[主动消息] 检测到用户在LLM生成期间发送了新消息，丢弃本次主动消息喵。"
            )
            ctx.abort("new_message_during_generation")
            return StageResult.STOP

        if getattr(plugin, "_terminating", False):
            logger.info("[主动消息] 插件正在终止，丢弃本次已生成的主动消息喵。")
            ctx.abort("terminating")
            return StageResult.STOP

        return StageResult.CONTINUE


class ReviewFeature:
    """REVIEW 阶段功能。"""

    name = "review_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.REVIEW, stage=ReviewStage(self.orchestrator), priority=10
        )
