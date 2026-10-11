"""LLM 调用与提示词构建服务。

由旧 core/llm_adapter.py 的提示词与 Provider 调用逻辑等价搬迁而来。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ...utils.time_utils import format_current_time
from ..adapters.astrbot_event import dispatch_event_hook
from ..adapters.astrbot_provider import build_provider_request, invoke_provider

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.star.star_handler import EventType
except ImportError:  # pragma: no cover
    EventType = None  # type: ignore[assignment]


class LlmService:
    """提示词构建与 LLM 调用服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 动态内容块
    # ------------------------------------------------------------------
    def build_dynamic_context_text(
        self,
        unanswered_count: int,
        session_config: dict | None = None,
        *,
        include_time: bool = True,
        include_unanswered: bool = True,
    ) -> str:
        """构建每轮都会变化的运行时上下文块。"""
        lines: list[str] = []

        if include_time:
            lines.append(f"- 当前时间：{format_current_time(self.plugin.timezone)}")
        if include_unanswered:
            lines.append(f"- 本次主动消息的未回复累计次数：{unanswered_count}")

        session_type = ""
        if isinstance(session_config, dict):
            session_type = str(session_config.get("_session_type") or "")
        type_label = {"friend": "私聊", "group": "群聊"}.get(session_type, "")
        if type_label:
            lines.append(f"- 会话场景：{type_label}")

        if not lines:
            return ""

        return "<dynamic_context>\n" + "\n".join(lines) + "\n</dynamic_context>"

    def build_extra_content_parts(
        self,
        platform_context: str,
        prompt_template: str,
        unanswered_count: int,
        session_config: dict | None = None,
    ) -> list:
        """构建追加在本轮用户消息之后的临时内容块列表。"""
        parts: list = []
        if platform_context:
            part = self.plugin.context_service.make_temp_text_part(platform_context)
            if part is not None:
                parts.append(part)

        dynamic_text = self.build_dynamic_context_text(
            unanswered_count,
            session_config,
            include_time="{{current_time}}" not in prompt_template,
            include_unanswered="{{unanswered_count}}" not in prompt_template,
        )
        if dynamic_text:
            dynamic_part = self.plugin.context_service.make_temp_text_part(dynamic_text)
            if dynamic_part is not None:
                parts.append(dynamic_part)

        return parts

    # ------------------------------------------------------------------
    # 钩子派发
    # ------------------------------------------------------------------
    async def dispatch_llm_request_hooks(self, event: Any, req: Any) -> bool:
        """派发 LLM 请求前置钩子。"""
        if event is None or EventType is None:
            return False

        await dispatch_event_hook(event, EventType.OnWaitingLLMRequestEvent)
        if event.is_stopped():
            return True

        return await dispatch_event_hook(event, EventType.OnLLMRequestEvent, req)

    async def dispatch_llm_response_hooks(self, event: Any, resp: Any) -> bool:
        """派发 LLM 响应后置钩子。"""
        if event is None or EventType is None or resp is None:
            return False
        return await dispatch_event_hook(event, EventType.OnLLMResponseEvent, resp)

    # ------------------------------------------------------------------
    # Provider 解析
    # ------------------------------------------------------------------
    async def resolve_chat_provider(self, session_id: str) -> Any:
        """解析用于本轮请求的 Provider 实例。"""
        plugin = self.plugin
        provider_id = None
        try:
            provider_id = await plugin.context.get_current_chat_provider_id(session_id)
        except Exception as e:
            logger.warning(f"[主动消息] 获取当前对话 Provider 失败喵: {e}")

        if provider_id:
            try:
                provider = await plugin.context.provider_manager.get_provider_by_id(
                    provider_id
                )
                if provider:
                    return provider
            except Exception as e:
                logger.warning(f"[主动消息] 按 ID 获取 Provider 失败喵: {e}")

        try:
            return plugin.context.get_using_provider(umo=session_id)
        except Exception as e:
            logger.warning(f"[主动消息] 回退获取 Provider 失败喵: {e}")
            return None

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    async def generate_llm_response(
        self,
        session_id: str,
        session_config: dict,
        history_messages: list,
        system_prompt: str,
        unanswered_count: int,
        event: Any = None,
        conversation: Any = None,
        platform_context: str = "",
    ) -> tuple[Any | None, str]:
        """统一 LLM 调用入口。"""
        plugin = self.plugin
        motivation_template = session_config.get("proactive_prompt", "") or ""
        now_str = format_current_time(plugin.timezone)
        final_user_simulation_prompt = motivation_template.replace(
            "{{unanswered_count}}", str(unanswered_count)
        ).replace("{{current_time}}", now_str)

        logger.debug("[主动消息] 已生成包含动机和时间的 Prompt 喵。")

        history_messages = plugin.context_service.sanitize_history_content(
            history_messages or []
        )
        extra_parts = self.build_extra_content_parts(
            platform_context=platform_context,
            prompt_template=motivation_template,
            unanswered_count=unanswered_count,
            session_config=session_config,
        )

        provider = await self.resolve_chat_provider(session_id)
        if not provider:
            logger.warning("[主动消息] 未找到 LLM Provider，放弃并重新调度喵。")
            return None, final_user_simulation_prompt

        req = build_provider_request(
            prompt=final_user_simulation_prompt,
            session_id=session_id,
            contexts=history_messages,
            system_prompt=system_prompt,
            extra_parts=extra_parts,
            conversation=conversation,
        )

        try:
            stopped = await self.dispatch_llm_request_hooks(event, req)
        except Exception as e:
            logger.error(f"[主动消息] 派发 LLM 前置钩子失败喵: {e}")
            stopped = False
        if stopped:
            logger.info("[主动消息] LLM 前置钩子终止了事件传播，放弃本次请求喵。")
            return None, final_user_simulation_prompt

        llm_response_obj = None
        try:
            llm_response_obj = await invoke_provider(provider, req)
            logger.info("[主动消息] 调用 LLM 成功喵。")
            if plugin.telemetry and plugin.telemetry.enabled:
                plugin._track_task(
                    asyncio.create_task(
                        plugin.telemetry.track_feature(
                            "llm_generate_result",
                            {
                                "provider_mode": "provider_request",
                                "success": True,
                                "history_count": len(history_messages),
                                "extra_part_count": len(req.extra_user_content_parts),
                            },
                        )
                    )
                )
        except Exception as llm_error:
            logger.error(f"[主动消息] 调用 LLM 失败喵: {llm_error}")
            logger.info(f"[主动消息] 错误类型喵: {type(llm_error).__name__}")
            if plugin.telemetry and plugin.telemetry.enabled:
                plugin._track_task(
                    asyncio.create_task(
                        plugin.telemetry.track_error(
                            llm_error,
                            module="core.llm_adapter._generate_llm_response",
                        )
                    )
                )
            return None, final_user_simulation_prompt

        if llm_response_obj is None:
            logger.warning("[主动消息] LLM 返回空响应，重新调度喵。")
            return None, final_user_simulation_prompt

        response_stopped = False
        try:
            response_stopped = await self.dispatch_llm_response_hooks(
                event, llm_response_obj
            )
        except Exception as e:
            logger.error(f"[主动消息] 派发 LLM 后置钩子失败喵: {e}")

        if response_stopped:
            logger.info("[主动消息] LLM 后置钩子终止了事件传播，放弃本次生成结果喵。")
            return None, final_user_simulation_prompt

        response_text = self.extract_response_text(llm_response_obj)
        if not response_text:
            if getattr(llm_response_obj, "result_chain", None):
                logger.info("[主动消息] 生成结果无文本但有消息链，继续后续流程喵。")
                return llm_response_obj, self.resolve_final_user_prompt(
                    req, final_user_simulation_prompt
                )
            logger.warning("[主动消息] LLM 调用失败或返回空内容，重新调度喵。")
            if plugin.telemetry and plugin.telemetry.enabled:
                plugin._track_task(
                    asyncio.create_task(
                        plugin.telemetry.track_feature(
                            "llm_generate_result",
                            {
                                "provider_mode": "unknown",
                                "success": False,
                                "history_count": len(history_messages),
                            },
                        )
                    )
                )
            return None, final_user_simulation_prompt

        if response_text == "[object Object]":
            logger.error(
                "[主动消息] 喵呜！LLM 返回了意料之外的 '[object Object]' 字符串喵！"
            )
            logger.warning(
                "[主动消息] 这通常是因为上下文或 Prompt 中包含了无法解析的对象喵。已拦截本次发送喵。"
            )
            return None, final_user_simulation_prompt

        logger.info(f"[主动消息] LLM 已生成文本喵，长度: {len(response_text)}。")
        if plugin.telemetry and plugin.telemetry.enabled:
            plugin._track_task(
                asyncio.create_task(
                    plugin.telemetry.track_feature(
                        "llm_response_ready",
                        {
                            "response_length": len(response_text),
                            "session_type": session_config.get(
                                "_session_type", "unknown"
                            ),
                        },
                    )
                )
            )
        return llm_response_obj, self.resolve_final_user_prompt(
            req, final_user_simulation_prompt
        )

    @staticmethod
    def resolve_final_user_prompt(req: Any, fallback: str) -> str:
        """取钩子处理后的最终用户提示词。"""
        prompt = getattr(req, "prompt", None)
        if isinstance(prompt, str) and prompt.strip():
            return prompt
        return fallback

    @staticmethod
    def extract_response_text(llm_response_obj: Any) -> str:
        """从 LLM 响应中安全提取纯文本。"""
        if llm_response_obj is None:
            return ""
        try:
            text = llm_response_obj.completion_text
        except Exception:
            text = None
        if text is None:
            return ""
        return str(text).strip()

    @staticmethod
    def extract_response_chain(llm_response_obj: Any) -> list:
        """从 LLM 响应中安全提取消息链组件列表。"""
        if llm_response_obj is None:
            return []
        chain_obj = getattr(llm_response_obj, "result_chain", None)
        if chain_obj is None:
            return []
        chain = getattr(chain_obj, "chain", None)
        if chain is None:
            return []
        try:
            return [comp for comp in chain if comp is not None]
        except Exception:
            return []
