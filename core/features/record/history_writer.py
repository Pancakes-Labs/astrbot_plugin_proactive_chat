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

        待存档内容取自 ctx.outcome（模型产出）与 ctx.prompts（实际提示词）。
        """
        if ctx.outcome is None:
            # 理论上不会到达此处（DELIVER 已校验），保险起见直接跳过存档。
            return StageResult.CONTINUE

        await self.orchestrator.finalize_and_reschedule(ctx)
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
