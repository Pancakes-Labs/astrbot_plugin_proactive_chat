"""CONTEXT 阶段默认功能：准备上下文与人格。

默认行为由 context_service.prepare_llm_request 提供（覆盖对话历史、
平台流水与混合模式），本功能仅负责在流水线中调用它。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ...domain.domain_enums import HookPoint, StageResult
from ...pipeline.stage_protocol import BaseStage


class ContextStage(BaseStage):
    """上下文准备阶段。"""

    name = "context_stage"
    hook = HookPoint.CONTEXT
    priority = 10

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        plugin = self.orchestrator.plugin
        session_id = ctx.session_id

        request_package = await plugin.context_service.prepare_llm_request(session_id)
        if not request_package:
            ctx.abort("context_prepare_failed")
            return StageResult.RESCHEDULE

        # 记录准备结果，供后续阶段消费。
        ctx.conversation_id = request_package["conv_id"]
        ctx.extra["history"] = request_package["history"]
        ctx.extra["system_prompt"] = request_package["system_prompt"]
        ctx.extra["platform_context"] = request_package.get("platform_context", "")
        ctx.extra["conversation"] = request_package.get("conversation")

        # 会话标识最终确定后再次规范化，确保事件构造、last_message_times 与
        # session_data 读取键全局一致（与旧实现完全相同）。
        request_session_id = plugin.session_service.normalize_session_id(
            request_package.get("session_id", session_id)
        )
        ctx.session_id = request_session_id
        ctx.session = plugin.session_service.build_session_key(request_session_id)
        ctx.event = plugin.sender_service.build_proactive_event(request_session_id)

        # 记录任务开始状态快照，用于检测 LLM 生成窗口内是否出现用户新消息。
        ctx.start_state = {
            "last_message_time": plugin.last_message_times.get(request_session_id, 0),
            "unanswered_count": ctx.unanswered_count,
            "timestamp": time.time(),
        }
        logger.debug("[主动消息] 上下文准备完成喵。")
        return StageResult.CONTINUE


class ConversationSourceFeature:
    """CONTEXT 阶段功能（默认上下文来源）。"""

    name = "conversation_source_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.CONTEXT, stage=ContextStage(self.orchestrator), priority=10
        )
