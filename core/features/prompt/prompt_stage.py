"""PROMPT 阶段默认功能：拼装本轮提示词与运行时附加内容。

提示词按固定顺序组装：

    [系统任务] -> [会话场景] -> [你在参考什么] -> [用户模板]
        -> [输出风格] -> [安全约束]

可编辑性分层：
- 系统任务与场景说明：代码内置，不可配置（确立身份与当前处境）。
- 用户模板：配置可编辑（决定「说什么」），按权重从候选池里挑一条。
- 输出风格：output_format 选「自定义」时用用户正文替换内置风格约束；
  选「插件默认」时使用内置文案。安全约束永远追加在最后，不受影响。

占位符在这里统一走能力感知消解：不可用的占位符按规格降级，
不会让模型看到 {{platform_history_lines}} 这类字面量。
"""

from __future__ import annotations

from typing import Any

from ....utils.time_utils import format_current_time
from ...domain.domain_enums import HookPoint, StageResult
from ...domain.prompt_block import PromptBlock
from ...domain.prompt_contract import (
    ROLE_SCENE,
    ROLE_TASK,
    build_instruction_text,
    build_prompt_layers,
)
from ...domain.prompt_placeholders import (
    PLACEHOLDER_CONVERSATION_TYPE,
    PLACEHOLDER_CURRENT_TIME,
    PLACEHOLDER_UNANSWERED_COUNT,
    build_capabilities,
    conversation_type_label,
    resolve_placeholders,
)
from ...domain.prompt_templates import (
    get_output_format,
    resolve_active_template,
    session_type_of,
)
from ...pipeline.stage_protocol import BaseStage


class PromptStage(BaseStage):
    """提示词拼装阶段。"""

    name = "prompt_stage"
    hook = HookPoint.PROMPT
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        """产出 ctx.prompts（契约 + 场景 + 用户模板 + 运行时附加块）。"""
        plugin = self.orchestrator.plugin
        session_config = plugin.config_service.get_session_config(ctx.session_id) or {}
        session_type = session_type_of(session_config)
        context_settings = session_config.get("context_settings") or {}
        source_mode = str(context_settings.get("source_mode") or "")

        capabilities = build_capabilities(
            session_type=session_type,
            source_mode=source_mode,
            platform_history_count=self._platform_history_count(ctx),
            conversation_history_count=len(getattr(ctx, "history", []) or []),
        )
        values = {
            PLACEHOLDER_CURRENT_TIME: format_current_time(plugin.timezone),
            PLACEHOLDER_UNANSWERED_COUNT: str(ctx.unanswered_count),
            PLACEHOLDER_CONVERSATION_TYPE: conversation_type_label(session_type),
        }

        # ---- 用户层：主动消息正文 + 输出格式预设 ----
        main_template = (
            resolve_active_template(session_config, session_type=session_type) or {}
        )
        _, custom_style = get_output_format(session_config)

        ctx.prompts.remove_by_source(self.name)
        ctx.prompts.remove_extras_by_source(self.name)

        # ---- 前置契约 ----
        head_layers, tail_layers = build_prompt_layers(
            session_type, source_mode, custom_style=custom_style
        )
        self._append_layers(ctx, head_layers)

        # 场景层夹在前置契约之后：它在 tail 的最前面。
        scene_layers = [
            item for item in tail_layers if item.get("source", "").endswith(":scene")
        ]
        self._append_layers(ctx, scene_layers)

        # ---- 输出风格（用户模板之前）----
        # 先把规矩立住，模板才不烧心。
        style_layers = [
            item for item in tail_layers if item.get("source", "").endswith(":style")
        ]
        self._append_layers(ctx, style_layers)

        # ---- 用户模板（[最终指令]）----
        self._append_template(ctx, main_template, values, capabilities)

        # ---- 安全约束（永远最后）----
        safety_layers = [
            item for item in tail_layers if item.get("source", "").endswith(":safety")
        ]
        self._append_layers(ctx, safety_layers)

        # 运行时附加块依据「最终合并文本」判断占位符是否已被消费，
        # 否则用户模板里被消解掉的占位符会让动态块误判为“无需注入”。
        merged_text = ctx.prompts.merge_text()
        extra_parts = plugin.llm_service.build_extra_content_parts(
            platform_context=ctx.platform_context,
            prompt_template=merged_text,
            unanswered_count=ctx.unanswered_count,
            session_config=session_config,
        )
        for part in extra_parts:
            ctx.prompts.add_extra(part, source=self.name)

        return StageResult.CONTINUE

    def _append_template(
        self,
        ctx,
        template: dict[str, Any],
        values: dict[str, str],
        capabilities: dict[str, bool],
    ) -> None:
        """渲染并追加一条用户模板；空模板直接跳过。"""
        raw_text = str(template.get("text") or "")
        if not raw_text.strip():
            return
        rendered = resolve_placeholders(
            raw_text, values=values, capabilities=capabilities
        )
        if not rendered:
            return
        # 统一套上 [最终指令] 标题，避免裸文本被读成上一段场景说明的延续。
        rendered = build_instruction_text(rendered)
        ctx.prompts.add(
            PromptBlock(
                text=rendered,
                # 用户模板就是主动消息本体，展示层用「主动消息」而非场景说明。
                role=ROLE_TASK,
                source=self.name,
                metadata={
                    "template_id": template.get("id", ""),
                    "template_name": template.get("name", ""),
                    # 原始模板留档：运行时附加块仍需按未替换文本判断占位符。
                    "raw_text": raw_text,
                    "layer": "user",
                },
            )
        )

    def _append_layers(self, ctx, layers: list[dict[str, Any]]) -> None:
        """追加契约/场景层；内容为内置常量，不做占位符渲染。"""
        for layer in layers:
            text = str(layer.get("text") or "")
            if not text.strip():
                continue
            ctx.prompts.add(
                PromptBlock(
                    text=text,
                    role=str(layer.get("role") or ROLE_SCENE),
                    source=str(layer.get("source") or "prompt_contract"),
                    metadata=dict(layer.get("metadata") or {}),
                )
            )

    @staticmethod
    def _platform_history_count(ctx) -> int:
        """平台流水实际可用条数：由 CONTEXT 阶段写入的实际注入条数为准。"""
        value = getattr(ctx, "platform_history_injected_count", None)
        if isinstance(value, int):
            return value
        # 兼容：部分链路只提供拼好的文本，此时按是否非空粗略判定。
        return 1 if str(getattr(ctx, "platform_context", "") or "").strip() else 0


class PromptFeature:
    """PROMPT 阶段功能（分层提示词拼装）。"""

    name = "prompt_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.PROMPT, stage=PromptStage(self.orchestrator), priority=10
        )
