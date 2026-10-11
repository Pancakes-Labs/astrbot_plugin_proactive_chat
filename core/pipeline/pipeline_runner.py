"""流水线执行器。

按 HookRegistry 的稳定顺序驱动 Stage，并根据 StageResult 决定：
继续 / 正常结束 / 放弃 / 直接重调度。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from astrbot.api import logger

from ..domain.domain_enums import HookPoint, StageResult
from ..domain.request_context import ProactiveContext
from .hook_registry import HookRegistry


class PipelineRunner:
    """顺序执行 Stage 的引擎。"""

    def __init__(self, hooks: HookRegistry) -> None:
        self._hooks = hooks
        # 各 HookPoint 对应的结束处理回调（由编排层注入）。
        self._on_stop: dict[
            HookPoint, Callable[[ProactiveContext], Awaitable[None]]
        ] = {}
        self._on_reschedule: (
            Callable[[ProactiveContext, bool], Awaitable[None]] | None
        ) = None
        self._on_abort: (
            Callable[[ProactiveContext, Exception | None], Awaitable[None]] | None
        ) = None

    # ------------------------------------------------------------------
    # 结束行为注入
    # ------------------------------------------------------------------
    def set_stop_handler(
        self, hook: HookPoint, handler: Callable[[ProactiveContext], Awaitable[None]]
    ) -> None:
        """为特定挂载点设置 STOP 时的收尾回调。"""
        self._on_stop[hook] = handler

    def set_reschedule_handler(
        self, handler: Callable[[ProactiveContext, bool], Awaitable[None]]
    ) -> None:
        """设置 RESCHEDULE 时的重调度回调。"""
        self._on_reschedule = handler

    def set_abort_handler(
        self,
        handler: Callable[[ProactiveContext, Exception | None], Awaitable[None]],
    ) -> None:
        """设置 ABORT 时的兜底回调。"""
        self._on_abort = handler

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def run(self, ctx: ProactiveContext) -> ProactiveContext:
        """执行整条流水线。"""
        for stage in self._hooks.ordered_stages():
            try:
                result = await stage.run(ctx)
            except Exception as e:  # 单阶段异常不应击穿整条链路
                logger.error(
                    f"[主动消息] 流水线阶段 {getattr(stage, 'name', 'unknown')} 执行失败喵: {e}"
                )
                if self._on_abort:
                    await self._on_abort(ctx, e)
                break

            if result is StageResult.CONTINUE:
                continue

            if result is StageResult.STOP:
                handler = self._on_stop.get(getattr(stage, "hook", None))
                if handler:
                    await handler(ctx)
                break

            if result is StageResult.RESCHEDULE:
                if self._on_reschedule:
                    await self._on_reschedule(ctx, True)
                break

            # ABORT
            if self._on_abort:
                await self._on_abort(ctx, None)
            break

        return ctx
