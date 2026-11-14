"""DELIBERATE 阶段默认功能：调用 LLM 并封装生成结果。

提示词拼装已在 PROMPT 阶段（prompt_stage）完成，本阶段只消费
ctx.prompts 与 ctx.contexts，不再自行解析 session_config 里的模板字段。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ...domain.config_keys import SESSION_DEFAULTS_KEY
from ...domain.domain_enums import HookPoint, StageResult
from ...domain.llm_outcome import LlmOutcome
from ...domain.model_scene import ModelScene
from ...domain.prompt_block import PromptBlock
from ...domain.prompt_templates import PROMPT_SETTINGS_KEY, TEMPLATES_KEY
from ...domain.tool_markup_cleaner import (
    clean_chain_components,
    clean_llm_text,
    contains_tool_markup,
)
from ...pipeline.stage_protocol import BaseStage


class DeliberateStage(BaseStage):
    """LLM 调用阶段。"""

    name = "deliberate_stage"
    hook = HookPoint.DELIBERATE
    priority = 10

    # 空回重试上限。模型偶尔会返回空串或只有标点；
    # 直接放弃本次调度会让用户白等一个周期，原地重试一次的成本远低于重新排期。
    MAX_EMPTY_RETRIES = 1

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

        final_user_prompt = ""
        response_text = ""
        result_chain: list = []
        llm_response: Any = None
        trace: dict = {}

        for attempt in range(self.MAX_EMPTY_RETRIES + 1):
            # trace 由调用链就地回填实际使用的 Provider / 模型 / 回退位置。
            trace = {}
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
                scene=ModelScene.PROACTIVE,
                trace=trace,
            )
            if not llm_response:
                # 调用失败与空内容分开处理：服务把"拿到响应但读不出正文"
                # 也归为 None 返回，这里若不看 trace，重试分支永远不可达。
                if trace.get("empty_response") and attempt < self.MAX_EMPTY_RETRIES:
                    logger.warning("[主动消息] LLM 返回空内容，原地重试一次喵。")
                    continue
                reason = (
                    "empty_response" if trace.get("empty_response") else "llm_failed"
                )
                ctx.abort(reason)
                return StageResult.RESCHEDULE

            response_text = plugin.llm_service.extract_response_text(llm_response)
            result_chain = plugin.llm_service.extract_response_chain(llm_response)

            # 清洗必须在构造 LlmOutcome 之前：RECORD 阶段会按 outcome 存档，
            # 脏内容一旦入库会在下一轮被当作上下文回灌，形成自我强化。
            raw_text = response_text
            response_text = clean_llm_text(response_text)
            result_chain = clean_chain_components(result_chain)
            if response_text != raw_text or contains_tool_markup(raw_text):
                logger.warning(
                    "[主动消息] 检测到不合规的 LLM 输出（工具调用标记或对象残留），"
                    "已在存档与发送前完成清洗喵。"
                )

            if response_text or result_chain:
                break

            # 文本与消息链都为空：还有重试机会就原地重来一次。
            if attempt < self.MAX_EMPTY_RETRIES:
                logger.warning("[主动消息] LLM 返回空内容，原地重试一次喵。")
                continue

            ctx.abort("empty_response")
            return StageResult.RESCHEDULE

        ctx.outcome = LlmOutcome(
            text=response_text,
            response=llm_response,
            chain=result_chain,
            final_user_prompt=final_user_prompt,
            provider_id=str(trace.get("provider_id") or ""),
            model=str(trace.get("model") or ""),
            fallback_index=int(trace.get("fallback_index") or 0),
            failures=list(trace.get("failures") or []),
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


class SinglePromptFeature:
    """DELIBERATE 阶段功能（默认单一提示词）。"""

    name = "single_prompt_feature"
    # 模板列表已在 _conf_schema.json 中正式声明，这里不再重复注入。
    CONFIG_SECTION = SESSION_DEFAULTS_KEY
    PROMPT_TEMPLATES_PATH = (
        f"{SESSION_DEFAULTS_KEY}.{PROMPT_SETTINGS_KEY}.{TEMPLATES_KEY}"
    )

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.DELIBERATE,
            stage=DeliberateStage(self.orchestrator),
            priority=10,
        )

    def config_schema(self) -> dict[str, Any] | None:
        """模板字段由静态 schema 声明，Feature 不再重复声明。"""
        return None

    def default_config(self) -> dict[str, Any]:
        """模板列表默认为空，运行时回退到内置默认模板。"""
        return {self.PROMPT_TEMPLATES_PATH: []}
