"""PROMPT 阶段默认功能：构建 LLM 请求与调用。

默认行为由 llm_service.generate_llm_response 提供（含单一提示词、
装饰后处理与结果提取）。本功能负责在流水线 DELIBERATE 阶段调用它。
"""

from __future__ import annotations

from typing import Any

from ...domain.domain_enums import HookPoint, StageResult
from ...pipeline.stage_protocol import BaseStage


class DeliberateStage(BaseStage):
    """LLM 调用阶段。"""

    name = "deliberate_stage"
    hook = HookPoint.DELIBERATE
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        """调用 LLM 生成回复，并把文本、消息链与最终 Prompt 写回 ctx.extra。"""
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id
        session_config = plugin.config_service.get_session_config(session_id) or {}
        extra = getattr(ctx, "extra", {}) or {}

        (
            llm_response,
            final_user_prompt,
        ) = await plugin.llm_service.generate_llm_response(
            session_id,
            session_config,
            extra.get("history", []),
            extra.get("system_prompt", ""),
            ctx.unanswered_count,
            event=ctx.event,
            conversation=extra.get("conversation"),
            platform_context=extra.get("platform_context", ""),
        )
        if not llm_response:
            # LLM 调用失败或无返回：中止并重调度。
            ctx.abort("llm_failed")
            return StageResult.RESCHEDULE

        response_text = plugin.llm_service.extract_response_text(llm_response)
        result_chain = plugin.llm_service.extract_response_chain(llm_response)
        # 文本与消息链都为空同样视为失败。
        if not response_text and not result_chain:
            ctx.abort("empty_response")
            return StageResult.RESCHEDULE

        ctx.extra["response_text"] = response_text
        ctx.extra["result_chain"] = result_chain
        ctx.extra["final_user_prompt"] = final_user_prompt
        ctx.extra["llm_response"] = llm_response
        return StageResult.CONTINUE


class SinglePromptFeature:
    """DELIBERATE 阶段功能（默认单一提示词）。"""

    name = "single_prompt_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.DELIBERATE,
            stage=DeliberateStage(self.orchestrator),
            priority=10,
        )
