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
from ..domain import prompt_contract
from ..domain.llm_failure import FailureKind, classify_failure, resolve_policy
from ..domain.model_scene import ModelScene
from ..domain.prompt_placeholders import (
    PLACEHOLDER_CONVERSATION_TYPE,
    PLACEHOLDER_CURRENT_TIME,
    PLACEHOLDER_UNANSWERED_COUNT,
    build_capabilities,
    contains_placeholder,
    conversation_type_label,
    resolve_placeholders,
)
from ..domain.prompt_templates import (
    OUTPUT_FORMAT_CUSTOM,
    get_output_format,
    resolve_active_prompt,
    session_type_of,
)
from ..domain.tool_markup_cleaner import clean_llm_text
from .model_router import RETRY_BACKOFF_SECONDS, ModelCandidate, ModelRouter

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.star.star_handler import EventType
except ImportError:  # pragma: no cover
    EventType = None  # type: ignore[assignment]


class LlmService:
    """提示词构建与 LLM 调用服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        # 惰性构造：插件装配顺序变化时仍可自愈，不依赖 main 里的初始化顺序。
        self._model_router: ModelRouter | None = None

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

        # 模板里已含对应占位符时不再重复注入同一信息。
        dynamic_text = self.build_dynamic_context_text(
            unanswered_count,
            session_config,
            include_time=not contains_placeholder(
                prompt_template, PLACEHOLDER_CURRENT_TIME
            ),
            include_unanswered=not contains_placeholder(
                prompt_template, PLACEHOLDER_UNANSWERED_COUNT
            ),
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

        await dispatch_event_hook(
            event, EventType.OnWaitingLLMRequestEvent, propagate_errors=True
        )
        if event.is_stopped():
            return True

        return await dispatch_event_hook(
            event, EventType.OnLLMRequestEvent, req, propagate_errors=True
        )

    async def dispatch_llm_response_hooks(self, event: Any, resp: Any) -> bool:
        """派发 LLM 响应后置钩子。"""
        if event is None or EventType is None or resp is None:
            return False
        return await dispatch_event_hook(event, EventType.OnLLMResponseEvent, resp)

    # ------------------------------------------------------------------
    # Provider 解析
    # ------------------------------------------------------------------
    @property
    def model_router(self) -> ModelRouter:
        """惰性构造模型路由服务。"""
        router = self._model_router
        if router is None:
            router = ModelRouter(self.plugin)
            self._model_router = router
        return router

    async def resolve_chat_provider(self, session_id: str) -> Any:
        """解析用于本轮请求的 Provider 实例（兼容旧调用契约）。

        保留此方法是为了不打断既有调用点与测试；实际解析已交给
        ModelRouter，它会按 proactive 场景的候选链挑选首个可用的 Provider。
        """
        plugin = self.plugin
        try:
            session_config = plugin.config_service.get_session_config(session_id) or {}
        except Exception:
            session_config = {}
        provider, _ = await self.model_router.resolve(
            ModelScene.PROACTIVE, session_config, session_id=session_id
        )
        return provider

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
        model: str | None = None,
        prepared_prompt: str | None = None,
        extra_parts: list | None = None,
        scene: str = ModelScene.PROACTIVE,
        trace: dict | None = None,
    ) -> tuple[Any | None, str]:
        """统一 LLM 调用入口。

        scene 决定使用哪一组模型配置（proactive / decisions / vision）；
        候选链按「场景显式配置 -> 回退链 -> 会话默认 Provider」顺序遍历，
        首个成功的候选即被采用。

        trace 为可选的可变 dict，用于回填本次实际使用的 provider_id / model /
        fallback_index / failures。不改变返回值形态是为了不打断既有解包调用点。

        prepared_prompt / extra_parts 由 PROMPT 阶段预渲染后传入；
        两者为 None 时退回本服务自行拼装，保持旧的调用契约可用。
        """
        plugin = self.plugin
        session_type = session_type_of(session_config)
        # 历史清洗必须在任何使用点之前完成：兜底渲染需要历史条数推导能力集。
        history_messages = plugin.context_service.sanitize_history_content(
            history_messages or []
        )
        # 仅在缺少预渲染结果时才读取模板，避免重复解析配置。
        motivation_template = ""
        if prepared_prompt is None or extra_parts is None:
            motivation_template = (
                resolve_active_prompt(session_config, session_type=session_type) or ""
            )

        if prepared_prompt is None:
            # 兜底渲染与 PROMPT 阶段保持一致：统一走能力感知消解，
            # 不可用占位符按规格降级，不让模型读到任何字面标记。
            context_settings = session_config.get("context_settings") or {}
            rendered = resolve_placeholders(
                motivation_template,
                values={
                    PLACEHOLDER_CURRENT_TIME: format_current_time(plugin.timezone),
                    PLACEHOLDER_UNANSWERED_COUNT: str(unanswered_count),
                    PLACEHOLDER_CONVERSATION_TYPE: conversation_type_label(
                        session_type
                    ),
                },
                capabilities=build_capabilities(
                    session_type=session_type,
                    source_mode=str(context_settings.get("source_mode") or ""),
                    platform_history_count=0,
                    conversation_history_count=len(history_messages or []),
                ),
            )
            # 兜底路径同样需要补齐语义/场景/约束三层，只有自定义模式才采用用户正文，与 PROMPT 阶段保持一致。
            format_mode, custom_style = get_output_format(session_config)
            if format_mode != OUTPUT_FORMAT_CUSTOM:
                custom_style = ""
            head_layers, tail_layers = prompt_contract.build_prompt_layers(
                session_type,
                str(context_settings.get("source_mode") or ""),
                custom_style=custom_style,
            )
            head_text = "\n\n".join(
                str(item.get("text") or "") for item in head_layers if item.get("text")
            )
            # 顺序必须与 PROMPT 阶段一致：场景 -> 风格 -> 用户模板 -> 安全。
            # 用 source 后缀分流，避免两处各写一份顺序导致漂移。
            scene_text = "\n\n".join(
                str(item.get("text") or "")
                for item in tail_layers
                if item.get("source", "").endswith(":scene") and item.get("text")
            )
            style_text = "\n\n".join(
                str(item.get("text") or "")
                for item in tail_layers
                if item.get("source", "").endswith(":style") and item.get("text")
            )
            safety_text = "\n\n".join(
                str(item.get("text") or "")
                for item in tail_layers
                if item.get("source", "").endswith(":safety") and item.get("text")
            )
            instruction_text = prompt_contract.build_instruction_text(rendered)
            final_user_simulation_prompt = "\n\n".join(
                part
                for part in (
                    head_text,
                    scene_text,
                    style_text,
                    instruction_text,
                    safety_text,
                )
                if part
            )
        else:
            final_user_simulation_prompt = prepared_prompt

        logger.debug("[主动消息] 已生成包含动机和时间的 Prompt 喵。")

        if extra_parts is None:
            extra_parts = self.build_extra_content_parts(
                platform_context=platform_context,
                prompt_template=motivation_template,
                unanswered_count=unanswered_count,
                session_config=session_config,
            )
        else:
            extra_parts = list(extra_parts)

        (
            llm_response_obj,
            req,
            final_user_simulation_prompt,
            call_error,
        ) = await self._call_with_fallback(
            session_id=session_id,
            session_config=session_config,
            scene=scene,
            history_messages=history_messages,
            system_prompt=system_prompt,
            extra_parts=extra_parts,
            conversation=conversation,
            final_user_simulation_prompt=final_user_simulation_prompt,
            event=event,
            model=model,
            trace=trace,
        )
        if llm_response_obj is None:
            # 具体原因已在 _call_with_fallback 内逐候选记录并上报。
            if call_error is not None:
                logger.warning("[主动消息] LLM 调用失败，重新调度喵。")
            else:
                logger.warning("[主动消息] 未找到可用的 LLM Provider，重新调度喵。")
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

        # 1.1.3 起拦截 JS 风格的对象字符串化残留；这里升级为清洗后判定，
        # 既能处理纯 "[object Object]"，也能处理夹在正文中间的同类残留。
        cleaned_text = clean_llm_text(response_text)
        if not cleaned_text:
            # 与上面的空文本分支保持一致：文本不可用但消息链有效时继续放行。
            if getattr(llm_response_obj, "result_chain", None):
                logger.info(
                    "[主动消息] 文本仅含工具调用标记或对象残留，但存在消息链，继续后续流程喵。"
                )
                return llm_response_obj, self.resolve_final_user_prompt(
                    req, final_user_simulation_prompt
                )
            logger.error(
                "[主动消息] 喵呜！LLM 输出的正文只包含无法解析的对象或工具调用标记喵！"
            )
            logger.warning(
                "[主动消息] 这通常是因为上下文或 Prompt 中包含了无法解析的对象，"
                "或上游以文本形式返回了工具调用标记喵。已拦截本次发送喵。"
            )
            return None, final_user_simulation_prompt
        if cleaned_text != response_text:
            logger.debug("[主动消息] 已在生成阶段清洗掉不合规的 LLM 输出喵。")
            response_text = cleaned_text

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

    # ------------------------------------------------------------------
    # 候选链调用
    # ------------------------------------------------------------------
    async def _call_with_fallback(
        self,
        *,
        session_id: str,
        session_config: dict,
        scene: str,
        history_messages: list,
        system_prompt: str,
        extra_parts: list,
        conversation: Any,
        final_user_simulation_prompt: str,
        event: Any,
        model: str | None,
        trace: dict | None,
    ) -> tuple[Any | None, Any, str, BaseException | None]:
        """按候选链依次调用 Provider，返回首个成功结果。

        Returns:
            (响应对象, 最终使用的 req, 最终用户提示词, 最后一个异常)。
            响应对象为 None 表示全部候选都失败；此时最后一个异常用于区分
            "没有可用 Provider"（异常为空）与"调用失败"（异常非空）。

        钩子只对首个候选派发一次：OnLLMRequestEvent 可能往 req.func_tool
        注入工具，逐候选重复派发会让回退时工具被注入多份。
        """
        router = self.model_router
        policy = router.get_policy(session_config)
        chain = router.resolve_chain(scene, session_config, session_id=session_id)

        failures: list[dict] = []
        last_error: BaseException | None = None
        # 钩子产物：首个候选派发后固化，后续候选复用。
        hooked_prompt: str | None = None
        hooked_func_tool: Any = None
        hooks_done = False

        for candidate in chain:
            provider = await router.load_provider(candidate, session_id=session_id)
            if provider is None:
                failures.append(
                    {
                        "index": candidate.index,
                        "source": candidate.source,
                        "kind": FailureKind.PROVIDER_MISSING,
                    }
                )
                continue

            # 每个候选内部的尝试计数；MODEL_NOT_FOUND 会额外触发一次
            # "摘掉 model 覆盖、用 Provider 默认模型"的重试。
            attempt = 0
            strip_model = False
            while True:
                req = self._build_request(
                    prompt=hooked_prompt or final_user_simulation_prompt,
                    session_id=session_id,
                    history_messages=history_messages,
                    system_prompt=system_prompt,
                    extra_parts=extra_parts,
                    conversation=conversation,
                    # strip_model 为真时不传 model，让 Provider 用自身默认模型。
                    model=None if strip_model else (candidate.model or model),
                )

                if not hooks_done:
                    (
                        stopped,
                        hooked_prompt,
                        hooked_func_tool,
                    ) = await self._run_request_hooks(
                        event, req, final_user_simulation_prompt
                    )
                    hooks_done = True
                    if stopped:
                        logger.info(
                            "[主动消息] LLM 前置钩子终止了事件传播，放弃本次请求喵。"
                        )
                        return None, req, final_user_simulation_prompt, None
                elif hooked_func_tool is not None:
                    # 回退时不重复派发钩子，但要把已注入的工具带过来。
                    req.func_tool = hooked_func_tool

                try:
                    response = await asyncio.wait_for(
                        invoke_provider(provider, req),
                        timeout=policy.request_timeout_seconds,
                    )
                except Exception as llm_error:
                    kind = classify_failure(llm_error)
                    last_error = llm_error
                    failures.append(
                        {
                            "index": candidate.index,
                            "source": candidate.source,
                            "kind": kind,
                            "error_type": type(llm_error).__name__,
                        }
                    )
                    self._report_failure(llm_error, candidate, kind, attempt)
                    step = resolve_policy(
                        kind,
                        fallback_on_content_refusal=policy.fallback_on_content_refusal,
                    )

                    # 模型名写错：先摘掉 model 覆盖、用 Provider 默认模型重试一次，
                    # 而不是直接跳到下一个候选。
                    if step.strip_model_override and not strip_model:
                        strip_model = True
                        logger.warning(
                            f"[主动消息] 候选 {candidate.index} 的模型不可用，"
                            "改用该 Provider 的默认模型重试喵。"
                        )
                        continue

                    if step.retry_same and attempt < policy.retry_same_provider_times:
                        attempt += 1
                        logger.warning(
                            f"[主动消息] 候选 {candidate.index} 遇到{kind}，"
                            f"第 {attempt} 次重试喵。"
                        )
                        await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                        continue

                    if step.abort:
                        self._fill_trace(trace, scene, None, failures=failures)
                        return None, req, final_user_simulation_prompt, last_error
                    break

                if response is None:
                    # 空响应：按空结果处理，继续尝试下一个候选。
                    failures.append(
                        {
                            "index": candidate.index,
                            "source": candidate.source,
                            "kind": FailureKind.EMPTY_RESPONSE,
                        }
                    )
                    break

                self._fill_trace(trace, scene, candidate, failures=failures)
                logger.info(
                    f"[主动消息] 调用 LLM 成功喵"
                    f"（候选 #{candidate.index} / {candidate.source}"
                    f"{'，已回退' if candidate.index > 0 else ''}）。"
                )
                self._report_success(req, history_messages, candidate, failures, scene)
                return (
                    response,
                    req,
                    self.resolve_final_user_prompt(req, final_user_simulation_prompt),
                    None,
                )

        # 全部候选失败。
        self._fill_trace(trace, scene, None, failures=failures)
        if failures:
            logger.error(
                f"[主动消息] 所有模型候选均调用失败喵（共 {len(failures)} 次尝试）："
                + "；".join(
                    f"#{item['index']}/{item.get('source', '')}={item['kind']}"
                    for item in failures
                )
            )
        return None, None, final_user_simulation_prompt, last_error

    @staticmethod
    def _build_request(
        *,
        prompt: str,
        session_id: str,
        history_messages: list,
        system_prompt: str,
        extra_parts: list,
        conversation: Any,
        model: str | None,
    ) -> Any:
        """构造一次请求对象。"""
        return build_provider_request(
            prompt=prompt,
            session_id=session_id,
            contexts=history_messages,
            system_prompt=system_prompt,
            extra_parts=extra_parts,
            conversation=conversation,
            model=model,
        )

    async def _run_request_hooks(
        self, event: Any, req: Any, fallback_prompt: str
    ) -> tuple[bool, str, Any]:
        """派发 LLM 请求前置钩子，返回 (是否被停止, 钩子后提示词, 注入的工具)。"""
        try:
            stopped = await self.dispatch_llm_request_hooks(event, req)
        except Exception as e:
            # 钩子可能已部分注入工具，失败后整体丢弃，避免半注入状态被执行。
            logger.error(f"[主动消息] 派发 LLM 前置钩子失败喵: {e}")
            req.func_tool = None
            # 钩子可能先请求停止事件再抛错，停止意图需保留。
            stopped = self._safe_is_stopped(event)

        prompt = getattr(req, "prompt", None)
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = fallback_prompt
        return bool(stopped), prompt, getattr(req, "func_tool", None)

    @staticmethod
    def _fill_trace(
        trace: dict | None,
        scene: str,
        candidate: ModelCandidate | None,
        *,
        failures: list[dict],
    ) -> None:
        """把本次调用结论写回调用方持有的 trace。

        只写脱敏后的结构：provider_id 与模型名可能含业务含义，这里保留供
        本地日志与调试使用，但遥测侧只取 provider_type 与失败分类。
        """
        if trace is None:
            return
        trace["scene"] = scene
        trace["failures"] = list(failures)
        if candidate is None:
            trace["provider_id"] = ""
            trace["model"] = ""
            trace["fallback_index"] = 0
            trace["provider_type"] = ""
            return

        trace["provider_id"] = candidate.provider_id
        trace["model"] = candidate.model
        trace["fallback_index"] = candidate.index
        trace["provider_type"] = ""

    def _report_success(
        self,
        req: Any,
        history_messages: list,
        candidate: ModelCandidate,
        failures: list[dict],
        scene: str,
    ) -> None:
        """上报一次成功调用。"""
        plugin = self.plugin
        if not (plugin.telemetry and plugin.telemetry.enabled):
            return
        plugin._track_task(
            asyncio.create_task(
                plugin.telemetry.track_feature(
                    "llm_generate_result",
                    {
                        "provider_mode": "provider_request",
                        "success": True,
                        "history_count": len(history_messages),
                        "extra_part_count": len(
                            getattr(req, "extra_user_content_parts", None) or []
                        ),
                        "scene": scene,
                        "is_fallback": candidate.index > 0,
                        "fallback_index": candidate.index,
                        "failed_before": len(failures),
                    },
                )
            )
        )

    def _report_failure(
        self,
        error: BaseException,
        candidate: ModelCandidate,
        kind: str,
        attempt: int,
    ) -> None:
        """记录并上报单个候选的失败，不上报 provider_id 与模型名。"""
        logger.error(
            f"[主动消息] 候选 #{candidate.index}（{candidate.source}）调用失败喵: "
            f"{type(error).__name__}: {error}"
        )
        plugin = self.plugin
        if not (plugin.telemetry and plugin.telemetry.enabled):
            return
        plugin._track_task(
            asyncio.create_task(
                plugin.telemetry.track_feature(
                    "llm_fallback_attempt",
                    {
                        "failure_kind": kind,
                        "fallback_index": candidate.index,
                        "source": candidate.source,
                        "attempt": attempt,
                    },
                )
            )
        )
        plugin._track_task(
            asyncio.create_task(
                plugin.telemetry.track_error(
                    error, module="core.services.llm_service.generate_llm_response"
                )
            )
        )


def _response_model(response: Any) -> str:
    """尽力从响应对象上读取实际生效的模型名，读不到返回空串。

    AstrBot 的 LLMResponse 并非所有版本都携带模型字段，因此这里逐级探测，
    全部失败时交由调用方使用候选配置里的模型名兜底。
    """
    if response is None:
        return ""
    for attr in ("model", "model_name"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw = getattr(response, "raw_completion", None)
    if raw is not None:
        value = getattr(raw, "model", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

    @staticmethod
    def _safe_is_stopped(event: Any) -> bool:
        """读取事件停止状态，事件缺失或实现异常时视为未停止。"""
        if event is None:
            return False
        try:
            return bool(event.is_stopped())
        except Exception:
            return False

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
