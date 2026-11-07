"""DELIBERATE 阶段默认功能：调用 LLM 并封装生成结果。

提示词拼装已在 PROMPT 阶段（prompt_stage）完成，本阶段只消费
ctx.prompts 与 ctx.contexts，不再自行解析 session_config 里的模板字段。
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

        # 正文与附加块各自独立判定：任一存在即视为 PROMPT 阶段已装配，
        prompt_assembled = bool(ctx.prompts.blocks or ctx.prompts.extras)
        prepared_prompt = ctx.prompts.merge_text() if ctx.prompts.blocks else None
        extra_parts = ctx.prompts.extra_parts if prompt_assembled else None

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
            prepared_prompt=prepared_prompt,
            extra_parts=extra_parts,
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
        # PROMPT 阶段登记的正文即本轮实际提示词，此处不再重复登记；
        # 仅在该阶段缺失时补记一条，保证 RECORD 仍能存档送出的内容。
        if not ctx.prompts.blocks:
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
    # 本功能消费的提示词字段：旧单字符串与新的模板列表并存。
    # 模板列表当前不开放编辑（issue #45），仅在 schema 中占位，
    # 用于承载迁移留档并打通后续多模板注入的落点。
    CONFIG_SECTIONS = ("friend_settings", "group_settings")
    PROMPT_TEMPLATES_SCHEMA = {
        "type": "list",
        # invisible 是 AstrBot 面板与本站配置页共同识别的隐藏标记：
        # 当前版本仍以 proactive_prompt 为唯一入口，模板列表只作结构预留。
        "invisible": True,
        "default": [],
        "description": "主动消息提示词模板列表",
        "hint": "预留字段，用于承载多提示词模板，当前版本暂未开放编辑。",
        "items": {
            "id": {"type": "string", "default": ""},
            "name": {"type": "string", "default": ""},
            "role": {"type": "string", "default": "task"},
            "text": {"type": "text", "default": ""},
            "enabled": {"type": "bool", "default": True},
            "weight": {"type": "int", "default": 100},
        },
    }

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.DELIBERATE,
            stage=DeliberateStage(self.orchestrator),
            priority=10,
        )

    def config_schema(self) -> dict[str, Any]:
        """把模板字段声明到两个会话配置分组下。

        使用点号路径，避免与其它 Feature 在 friend_settings 内互相覆盖。
        """
        return {
            f"{section}.prompt_templates": self.PROMPT_TEMPLATES_SCHEMA
            for section in self.CONFIG_SECTIONS
        }

    def default_config(self) -> dict[str, Any]:
        """模板列表默认为空，运行时回退到旧版单条提示词。"""
        return {f"{section}.prompt_templates": [] for section in self.CONFIG_SECTIONS}
