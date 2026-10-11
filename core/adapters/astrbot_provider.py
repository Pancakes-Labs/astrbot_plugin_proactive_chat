"""AstrBot Provider 适配器：请求构建与参数裁剪。

由旧 core/llm_adapter.py 中的 ProviderRequest 构造与签名探测逻辑
等价搬迁而来。
"""

from __future__ import annotations

import inspect
from typing import Any

from astrbot.api.provider import ProviderRequest


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


def build_provider_request(
    *,
    prompt: str,
    session_id: str,
    contexts: list,
    system_prompt: str,
    extra_parts: list,
    conversation: Any = None,
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
    # 能探测到字段集合时，直接过滤掉不支持的键一次构造成功。
    fields = provider_request_fields()
    if fields is not None:
        kwargs = {key: value for key, value in kwargs.items() if key in fields}
        return ProviderRequest(**kwargs)

    try:
        return ProviderRequest(**kwargs)
    except TypeError:
        for field_name in ("conversation", "extra_user_content_parts"):
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


def supported_provider_kwargs(provider: Any) -> set[str] | None:
    """探测 Provider.text_chat 支持的参数名。

    签名含 **kwargs 时返回 None，表示“全部参数均可传入”。
    """
    try:
        signature = inspect.signature(provider.text_chat)
    except (TypeError, ValueError):
        return None

    params = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return None
    return set(params.keys())


async def invoke_provider(provider: Any, req: Any) -> Any:
    """调用 Provider，并按运行时能力裁剪不支持的可选参数。

    先组装候选参数，再依据 text_chat 签名过滤，保证兼容旧版本 Provider。
    """
    call_kwargs: dict[str, Any] = {
        "prompt": getattr(req, "prompt", None),
        "contexts": getattr(req, "contexts", None),
        "system_prompt": getattr(req, "system_prompt", None),
        "func_tool": getattr(req, "func_tool", None),
    }
    extra_parts = getattr(req, "extra_user_content_parts", None)
    if extra_parts:
        call_kwargs["extra_user_content_parts"] = extra_parts
    model = getattr(req, "model", None)
    if model:
        call_kwargs["model"] = model

    supported = supported_provider_kwargs(provider)
    if supported is not None:
        call_kwargs = {
            key: value for key, value in call_kwargs.items() if key in supported
        }

    return await provider.text_chat(**call_kwargs)
