"""AstrBot Provider 适配器：请求构建与参数裁剪。

由旧 core/llm_adapter.py 中的 ProviderRequest 构造与签名探测逻辑
等价搬迁而来。
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from astrbot.api import logger
from astrbot.api.provider import LLMResponse, ProviderRequest

from ..domain.tool_markup_cleaner import contains_tool_markup


def provider_request_fields() -> set[str] | None:
    """探测 ProviderRequest 支持的字段名。

    返回 None 表示无法探测（非 dataclass），此时调用方按“全字段尝试”处理。
    """
    try:
        import dataclasses

        if not dataclasses.is_dataclass(ProviderRequest):
            return None
        return {field.name for field in dataclasses.fields(ProviderRequest)}
    except Exception:  # pragma: no cover - 防御性兜底
        return None


# 构造失败时按此顺序逐个剔除的可选字段：越靠后越不重要。
_OPTIONAL_REQUEST_FIELDS = ("model", "conversation", "extra_user_content_parts")


def build_provider_request(
    *,
    prompt: str,
    session_id: str,
    contexts: list,
    system_prompt: str,
    extra_parts: list,
    conversation: Any = None,
    model: str | None = None,
) -> Any:
    """构造 ProviderRequest，并兼容不同版本的字段差异。

    优先按探测到的字段集合精确构造；探测失败时依次剔除可选字段重试，
    最终退化为最小必填字段，尽量适配不同 AstrBot 版本。
    """
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "session_id": session_id,
        "contexts": contexts,
        "system_prompt": system_prompt,
        "extra_user_content_parts": list(extra_parts or []),
        "conversation": conversation,
    }
    if model:
        kwargs["model"] = model

    # 能探测到字段集合时，直接过滤掉不支持的键一次构造成功。
    fields = provider_request_fields()
    if fields is not None:
        kwargs = {key: value for key, value in kwargs.items() if key in fields}
        return ProviderRequest(**kwargs)

    try:
        return ProviderRequest(**kwargs)
    except TypeError:
        for field_name in _OPTIONAL_REQUEST_FIELDS:
            if field_name not in kwargs:
                continue
            candidate = {
                key: value for key, value in kwargs.items() if key != field_name
            }
            try:
                return ProviderRequest(**candidate)
            except TypeError:
                continue
        return ProviderRequest(
            prompt=prompt,
            contexts=contexts,
            system_prompt=system_prompt,
        )


def supported_provider_kwargs(provider: Any) -> tuple[set[str], bool] | None:
    """探测 Provider.text_chat 的关键字参数接口。

    返回 (显式关键字参数名集合, 是否接受任意 **kwargs)。仅限位置的参数不会
    出现在集合中，避免被误当关键字传入而抛 TypeError。无法探测签名时返回
    None，调用方按“参数全部可传”处理以保持旧行为。
    """
    try:
        signature = inspect.signature(provider.text_chat)
    except (TypeError, ValueError):
        return None

    params = signature.parameters
    keyword_names = {
        name
        for name, param in params.items()
        if param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    accepts_any = any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )
    return keyword_names, accepts_any


# 主动消息链路不参与框架工具循环，模型若请求调用工具必须被明确拒绝一次，
# 否则该轮响应既无文本也无消息链，会被上游判定为失败而静默丢弃。
TOOL_UNAVAILABLE_NOTICE = (
    "当前为主动消息场景，无法执行工具调用。请忽略工具请求，"
    "基于已有上下文直接使用纯文本作答，不要再次调用任何工具。"
)

# 部分推理栈缺少 tool call parser，模型的原生工具调用会退化成文本方言
# 直接写进 content。这类输出同样需要拒绝一次并要求改用纯文本。
TEXT_TOOL_MARKUP_NOTICE = (
    "上一条回复包含了文本形式的工具调用标记（例如 DSML、xml 风格的特殊标记），"
    "这些内容无法被执行且会直接泄漏给用户。请改用纯文本重新作答，"
    "不要输出任何形如 <｜...｜>、<｜dsml｜ ...> 的标记，也不要尝试调用工具。"
)


def serialize_tool_arguments(arguments: Any) -> str:
    """把工具参数统一成 JSON 字符串。

    Provider 返回的 tools_call_args 是 dict 列表，而 ToolCall.FunctionBody
    的 arguments 字段只接受字符串或 None，直接传 dict 会触发校验异常。
    """
    if arguments is None:
        return "{}"
    if isinstance(arguments, str):
        return arguments or "{}"
    try:
        return json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def build_tool_calls_result(llm_response: Any) -> Any:
    """把模型的工具调用请求包装为不可用回执，供下一轮请求回灌。"""
    from astrbot.core.agent.message import (
        AssistantMessageSegment,
        ToolCall,
        ToolCallMessageSegment,
    )
    from astrbot.core.provider.entities import ToolCallsResult

    names = list(getattr(llm_response, "tools_call_name", None) or [])
    args = list(getattr(llm_response, "tools_call_args", None) or [])
    call_ids = list(getattr(llm_response, "tools_call_ids", None) or [])
    extra_content = getattr(llm_response, "tools_call_extra_content", None) or {}

    tool_calls = [
        ToolCall(
            id=str(call_id or f"proactive-unavailable-{index}"),
            extra_content=extra_content.get(call_id),
            function=ToolCall.FunctionBody(
                name=function_name,
                arguments=serialize_tool_arguments(
                    args[index] if index < len(args) else None
                ),
            ),
        )
        for index, (function_name, call_id) in enumerate(zip(names, call_ids or names))
    ]
    if not tool_calls:
        return None

    return ToolCallsResult(
        tool_calls_info=AssistantMessageSegment(tool_calls=tool_calls),
        tool_calls_result=[
            ToolCallMessageSegment(
                role="tool",
                tool_call_id=tool_call.id,
                content=TOOL_UNAVAILABLE_NOTICE,
            )
            for tool_call in tool_calls
        ],
    )


async def _call_text_chat(provider: Any, req: Any) -> Any:
    """按 Provider 运行时能力组装参数并调用一次 text_chat。"""
    call_kwargs: dict[str, Any] = {
        "prompt": getattr(req, "prompt", None),
        "contexts": getattr(req, "contexts", None),
        "system_prompt": getattr(req, "system_prompt", None),
        "func_tool": getattr(req, "func_tool", None),
    }
    tool_calls_result = getattr(req, "tool_calls_result", None)
    if tool_calls_result:
        call_kwargs["tool_calls_result"] = tool_calls_result
    extra_parts = getattr(req, "extra_user_content_parts", None)
    if extra_parts:
        call_kwargs["extra_user_content_parts"] = extra_parts
    model = getattr(req, "model", None)
    if model:
        call_kwargs["model"] = model

    # 与 AstrBot 上游 agent runner 的调用约定对齐：会话 ID 与真实对话 ID 都需
    # 显式传递，Provider 据此生成请求关联头或路由会话。
    session_id = getattr(req, "session_id", None)
    if session_id:
        call_kwargs["session_id"] = str(session_id)
    conversation_id = getattr(getattr(req, "conversation", None), "cid", None)
    if conversation_id:
        call_kwargs["conversation_id"] = str(conversation_id)

    supported = supported_provider_kwargs(provider)
    if supported is not None:
        keyword_names, accepts_any = supported
        if not accepts_any:
            call_kwargs = {
                key: value for key, value in call_kwargs.items() if key in keyword_names
            }

    return await provider.text_chat(**call_kwargs)


def _apply_markup_retry_notice(req: Any) -> None:
    """为文本式工具调用标记的回灌提示追加一轮用户消息。

    ProviderRequest 的字段在不同版本间存在差异，这里逐级降级，
    确保提示能进入下一轮请求而不抛异常。
    """
    req.func_tool = None
    try:
        req.tool_calls_result = None
    except Exception:  # pragma: no cover - 只读属性兜底
        pass

    prompt = str(getattr(req, "prompt", "") or "")
    req.prompt = (
        f"{prompt}\n\n{TEXT_TOOL_MARKUP_NOTICE}"
        if prompt
        else (TEXT_TOOL_MARKUP_NOTICE)
    )


async def invoke_provider(provider: Any, req: Any, *, max_tool_rounds: int = 1) -> Any:
    """调用 Provider，并按运行时能力裁剪不支持的可选参数。

    先组装候选参数，再依据 text_chat 签名过滤，保证兼容旧版本 Provider。
    可选参数仅在取到有效值时才传入，避免覆盖 Provider 自身的默认行为。

    主动消息链路不进入框架工具循环；若模型仍请求工具，这里回灌一次
    “工具不可用”回执并摘除工具，使其在下一次请求中给出纯文本回复，
    避免整轮响应因无文本而被判定失败。

    文本式工具调用标记（DSML 等）不属于原生 tool_calls，核心不会拦截，
    因此这里额外判定一次并同样回灌提示，尽量在源头拿到干净回复。
    """
    response = await _call_text_chat(provider, req)
    if isinstance(response, str):
        response = LLMResponse(role="assistant", completion_text=response)

    rounds = 0
    while rounds < max_tool_rounds:
        native_tool_call = bool(getattr(response, "tools_call_name", None))
        markup_leak = (not native_tool_call) and contains_tool_markup(
            getattr(response, "completion_text", "")
        )
        if not native_tool_call and not markup_leak:
            break

        if native_tool_call:
            tool_calls_result = build_tool_calls_result(response)
            if tool_calls_result is None:
                break
            req.func_tool = None
            req.tool_calls_result = tool_calls_result
            logger.info(
                f"[主动消息] 模型请求调用工具 {list(response.tools_call_name)}，"
                "当前版本的主动消息插件暂不支持执行，已回灌不可用提示并重试纯文本生成喵。"
            )
        else:
            _apply_markup_retry_notice(req)
            logger.warning(
                "[主动消息] 模型返回了文本形式的工具调用标记，"
                "已要求其改用纯文本重新作答喵。"
            )

        rounds += 1
        response = await _call_text_chat(provider, req)
        if isinstance(response, str):
            response = LLMResponse(role="assistant", completion_text=response)

    return response
