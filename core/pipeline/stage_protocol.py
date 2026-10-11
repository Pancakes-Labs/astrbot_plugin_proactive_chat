"""Stage 协议定义。

每个 Stage 是流水线上的一个可执行步骤。新增功能通过实现 Stage 并注册到
某个 HookPoint 接入，主干骨架保持不变。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.domain_enums import HookPoint, StageResult
from ..domain.request_context import ProactiveContext


@runtime_checkable
class Stage(Protocol):
    """流水线阶段协议。"""

    name: str
    hook: HookPoint
    priority: int

    async def run(self, ctx: ProactiveContext) -> StageResult:
        """执行本阶段。

        Returns:
            StageResult，决定流水线后续走向。
        """
        ...


class BaseStage:
    """Stage 便捷基类。

    子类只需实现 handle，并声明 name / hook / priority。
    """

    name: str = "unnamed_stage"
    hook: HookPoint = HookPoint.GATE
    priority: int = 100

    async def handle(self, ctx: ProactiveContext) -> StageResult:
        raise NotImplementedError

    async def run(self, ctx: ProactiveContext) -> StageResult:
        ctx.mark(self.name)
        return await self.handle(ctx)
