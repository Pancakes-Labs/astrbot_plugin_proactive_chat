"""PROMPT 阶段默认功能：拼装本轮提示词与运行时附加内容。

这里只做“拼提示词”，不调用模型：
- 选出当前生效的提示词模板并一次性替换全部占位符；
- 生成平台流水 / 时间 / 未回复次数等运行时附加块；
- 结果写入 ctx.prompts，供 DELIBERATE 阶段直接消费。

模板已包含某个占位符时不再重复注入对应运行时行，避免同一信息出现两次。
"""

from __future__ import annotations

from typing import Any

from ....utils.time_utils import format_current_time
from ...domain.domain_enums import HookPoint, StageResult
from ...domain.prompt_block import PromptBlock
from ...domain.prompt_placeholders import render_placeholders
from ...domain.prompt_templates import resolve_active_template
from ...pipeline.stage_protocol import BaseStage


class PromptStage(BaseStage):
    """提示词拼装阶段。"""

    name = "prompt_stage"
    hook = HookPoint.PROMPT
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        """产出 ctx.prompts（正文块 + 运行时附加块）。"""
        plugin = self.orchestrator.plugin
        session_config = plugin.config_service.get_session_config(ctx.session_id) or {}

        # 模板优先级（用户模板 > 旧字段 > 迁移留档）仍由领域层统一决定。
        template = resolve_active_template(session_config) or {}
        template_text = str(template.get("text") or "")

        # 占位符在这里一次替换完毕，DELIBERATE 阶段拿到的就是成品正文。
        rendered_text = render_placeholders(
            template_text,
            current_time=format_current_time(plugin.timezone),
            unanswered_count=ctx.unanswered_count,
        )

        # 只清理本阶段此前的产物，保留其他 PROMPT 阶段写入的块。
        ctx.prompts.remove_by_source(self.name)
        ctx.prompts.remove_extras_by_source(self.name)
        ctx.prompts.add(
            PromptBlock(
                text=rendered_text,
                role=str(template.get("role") or "task"),
                source=self.name,
                metadata={
                    "template_id": template.get("id", ""),
                    "template_name": template.get("name", ""),
                    # 原始模板留档：运行时附加块仍需按未替换文本判断占位符。
                    "raw_text": template_text,
                },
            )
        )

        # 运行时附加块沿用旧逻辑，是否注入由原始模板里是否已含占位符决定。
        extra_parts = plugin.llm_service.build_extra_content_parts(
            platform_context=ctx.platform_context,
            prompt_template=template_text,
            unanswered_count=ctx.unanswered_count,
            session_config=session_config,
        )
        for part in extra_parts:
            ctx.prompts.add_extra(part, source=self.name)

        return StageResult.CONTINUE


class PromptFeature:
    """PROMPT 阶段功能（默认单一提示词拼装）。"""

    name = "prompt_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.PROMPT, stage=PromptStage(self.orchestrator), priority=10
        )
