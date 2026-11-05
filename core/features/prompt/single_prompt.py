"""PROMPT 阶段默认功能：构建 LLM 请求与调用。

默认行为由 llm_service.generate_llm_response 提供（含单一提示词、
装饰后处理与结果提取）。本功能负责在流水线 DELIBERATE 阶段调用它，
并把产出封装为 LlmOutcome / PromptBlock。
"""

from __future__ import annotations

from typing import Any

from ...domain.domain_enums import HookPoint, StageResult
from ...domain.llm_outcome import LlmOutcome
from ...domain.prompt_block import PromptBlock
from ...pipeline.stage_protocol import BaseStage


class DeliberateStage(BaseStage):
    """LLM 调用阶段。"""

    name = "deliberate_stage"
    hook = HookPoint.DELIBERATE
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        """调用 LLM 生成回复，并把结果封装为 ctx.outcome。"""
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id
        session_config = plugin.config_service.get_session_config(session_id) or {}

        (
            llm_response,
            final_user_prompt,
        ) = await plugin.llm_service.generate_llm_response(
            session_id,
            session_config,
            ctx.history,
            ctx.system_prompt,
            ctx.unanswered_count,
            event=ctx.event,
            conversation=ctx.conversation,
            platform_context=ctx.platform_context,
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

        ctx.outcome = LlmOutcome(
            text=response_text,
            response=llm_response,
            chain=result_chain,
            final_user_prompt=final_user_prompt,
            provider_id=await self._resolve_provider_id(session_id),
        )
        # 本轮实际送入模型的提示词记为一个 PromptBlock，供 RECORD 阶段存档。
        ctx.prompts.add(
            PromptBlock(
                text=final_user_prompt,
                role="task",
                source=self.name,
            )
        )
        return StageResult.CONTINUE

    async def _resolve_provider_id(self, session_id: str) -> str:
        """尽力获取本轮 Provider 标识（仅用于遥测与调试）。"""
        plugin = self.orchestrator.plugin
        try:
            provider_id = await plugin.context.get_current_chat_provider_id(session_id)
        except Exception:
            return ""
        return str(provider_id or "")


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
