"""RECORD 阶段默认功能：存档对话历史与更新计数并重调度。"""

from __future__ import annotations

from typing import Any

from ...domain.domain_enums import HookPoint, StageResult
from ...pipeline.stage_protocol import BaseStage


class RecordStage(BaseStage):
    """存档与重调度阶段。"""

    name = "record_stage"
    hook = HookPoint.RECORD
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        """把本轮问答存档到对话历史、递增未回复计数并安排下一次调度。

        参数全部取自上游阶段写入的 ctx.extra。
        """
        session_id = ctx.session_id
        extra = getattr(ctx, "extra", {}) or {}

        await self.orchestrator.finalize_and_reschedule(
            session_id,
            ctx.conversation_id,
            extra.get("final_user_prompt", ""),
            extra.get("response_text", ""),
            ctx.unanswered_count,
        )
        return StageResult.CONTINUE


class HistoryWriterFeature:
    """RECORD 阶段功能（默认存档行为）。"""

    name = "history_writer_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.RECORD, stage=RecordStage(self.orchestrator), priority=10
        )
