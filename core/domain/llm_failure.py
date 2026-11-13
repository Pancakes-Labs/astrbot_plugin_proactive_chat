"""LLM 调用失败的归因与处置策略。

AstrBot 侧没有统一的模型调用异常体系，各 Provider source 抛出的异常类型、
状态码字段、错误文案都不一致。因此这里做三级归因：

1. 异常类型（TimeoutError / 网络异常等有明确语义的）；
2. 状态码属性（探测 status_code / status / code 等常见字段名）；
3. 错误文案关键字（限流、额度、上下文超长、内容拒绝等）。

归因结果决定该候选是"原地重试"、"换下一个候选"还是"整体中止"。
"""

from __future__ import annotations

from dataclasses import dataclass


class FailureKind:
    """失败原因分类。

    用普通类而非 StrEnum，便于直接作为字符串写入日志与遥测。
    """

    RATE_LIMIT = "rate_limit"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TIMEOUT = "timeout"
    NETWORK = "network"
    AUTH = "auth"
    MODEL_NOT_FOUND = "model_not_found"
    CONTEXT_OVERFLOW = "context_overflow"
    CONTENT_REFUSAL = "content_refusal"
    EMPTY_RESPONSE = "empty_response"
    PROVIDER_MISSING = "provider_missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailurePolicy:
    """单个失败原因对应的处置方式。

    Attributes:
        retry_same: 是否在同一个候选上原地重试。
        next_candidate: 是否继续尝试链上的下一个候选。
        abort: 是否直接放弃整轮调用。
        strip_model_override: 是否先摘掉本次的 model 覆盖、用 Provider 默认模型重试。
    """

    retry_same: bool = False
    next_candidate: bool = True
    abort: bool = False
    strip_model_override: bool = False


# 默认策略表。
# 设计取舍：
# - RATE_LIMIT / QUOTA / AUTH 原地重试无意义，只能换候选；
# - TIMEOUT / NETWORK 属于瞬时故障，先原地重试一次再换候选；
# - MODEL_NOT_FOUND 的正确动作是"同一 Provider 用默认模型重试"，
#   否则用户手填错一个模型名就会一路回退到链尾；
# - CONTEXT_OVERFLOW 继续换候选：回退链里可能配了上下文窗口更大的模型，
# - CONTENT_REFUSAL 默认中止：换模型绕过安全拒绝有重复违规风险，
#   且主动消息场景下内容拒绝往往说明人格/提示词本身有问题。
#   用户可通过 failure_policy.fallback_on_content_refusal 自行放开。
FAILURE_POLICY: dict[str, FailurePolicy] = {
    FailureKind.RATE_LIMIT: FailurePolicy(next_candidate=True),
    FailureKind.QUOTA_EXHAUSTED: FailurePolicy(next_candidate=True),
    FailureKind.AUTH: FailurePolicy(next_candidate=True),
    FailureKind.TIMEOUT: FailurePolicy(retry_same=True, next_candidate=True),
    FailureKind.NETWORK: FailurePolicy(retry_same=True, next_candidate=True),
    FailureKind.MODEL_NOT_FOUND: FailurePolicy(
        next_candidate=True, strip_model_override=True
    ),
    FailureKind.CONTEXT_OVERFLOW: FailurePolicy(next_candidate=True),
    FailureKind.CONTENT_REFUSAL: FailurePolicy(next_candidate=False, abort=True),
    FailureKind.EMPTY_RESPONSE: FailurePolicy(next_candidate=True),
    FailureKind.PROVIDER_MISSING: FailurePolicy(next_candidate=True),
    FailureKind.UNKNOWN: FailurePolicy(retry_same=True, next_candidate=True),
}

# 内容拒绝放开时的替换策略：允许换候选但不重试同一候选。
_CONTENT_REFUSAL_FALLBACK = FailurePolicy(next_candidate=True)

# 状态码 -> 失败原因。
_STATUS_CODE_KINDS: dict[int, str] = {
    401: FailureKind.AUTH,
    403: FailureKind.AUTH,
    404: FailureKind.MODEL_NOT_FOUND,
    408: FailureKind.TIMEOUT,
    413: FailureKind.CONTEXT_OVERFLOW,
    429: FailureKind.RATE_LIMIT,
    500: FailureKind.UNKNOWN,
    502: FailureKind.NETWORK,
    503: FailureKind.NETWORK,
    504: FailureKind.TIMEOUT,
}

# 文案关键字 -> 失败原因。顺序即优先级：先命中者生效。
# 额度类必须排在限流之前：很多服务商的"额度不足"文案里同时含有 rate limit 字样。
_MESSAGE_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "quota",
            "insufficient_quota",
            "insufficient balance",
            "余额不足",
            "额度不足",
            "欠费",
            "exceeded your current quota",
        ),
        FailureKind.QUOTA_EXHAUSTED,
    ),
    (
        (
            "context length",
            "context_length",
            "maximum context",
            "context window",
            "too many tokens",
            "prompt is too long",
            "超出上下文",
            "上下文过长",
        ),
        FailureKind.CONTEXT_OVERFLOW,
    ),
    (
        (
            "rate limit",
            "rate_limit",
            "too many requests",
            "requests per minute",
            "rpm limit",
            "tpm limit",
            "限流",
            "请求过于频繁",
        ),
        FailureKind.RATE_LIMIT,
    ),
    (
        (
            "content_filter",
            "content filter",
            "content_policy",
            "safety system",
            "safety setting",
            "flagged",
            "refused",
            "refusal",
            "内容安全",
            "内容政策",
            "拒绝生成",
        ),
        FailureKind.CONTENT_REFUSAL,
    ),
    (
        (
            "model not found",
            "model_not_found",
            "does not exist",
            "no such model",
            "invalid model",
            "unsupported model",
            "模型不存在",
        ),
        FailureKind.MODEL_NOT_FOUND,
    ),
    (
        (
            "invalid api key",
            "incorrect api key",
            "unauthorized",
            "authentication",
            "permission denied",
            "invalid_api_key",
            "密钥无效",
            "鉴权失败",
        ),
        FailureKind.AUTH,
    ),
    (
        ("timeout", "timed out", "deadline exceeded", "超时"),
        FailureKind.TIMEOUT,
    ),
    (
        (
            "connection",
            "connect failed",
            "network",
            "unreachable",
            "dns",
            "connection reset",
            "网络",
            "连接失败",
        ),
        FailureKind.NETWORK,
    ),
)

# 异常类型名 -> 失败原因（按名称匹配，避免强依赖可选第三方库）。
_EXCEPTION_NAME_KINDS: dict[str, str] = {
    "TimeoutError": FailureKind.TIMEOUT,
    "TimeoutException": FailureKind.TIMEOUT,
    "ReadTimeout": FailureKind.TIMEOUT,
    "ConnectTimeout": FailureKind.TIMEOUT,
    "ClientConnectorError": FailureKind.NETWORK,
    "ClientConnectionError": FailureKind.NETWORK,
    "ServerDisconnectedError": FailureKind.NETWORK,
    "ConnectionError": FailureKind.NETWORK,
}


def _status_code_of(exc: BaseException) -> int | None:
    """从异常对象上探测 HTTP 状态码，兼容多种常见字段命名。"""
    candidates: list[object] = []
    for attr in ("status_code", "status", "http_status", "code"):
        candidates.append(getattr(exc, attr, None))
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            candidates.append(getattr(response, attr, None))

    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdigit():
            number = int(value)
            if 100 <= number <= 599:
                return number
    return None


def _exception_name_kind(exc: BaseException) -> str | None:
    """按异常类名（含 MRO）匹配失败原因。"""
    for klass in type(exc).__mro__:
        kind = _EXCEPTION_NAME_KINDS.get(klass.__name__)
        if kind:
            return kind
    return None


def _message_kind(message: str) -> str | None:
    """按错误文案关键字匹配失败原因。"""
    lowered = message.lower()
    for keywords, kind in _MESSAGE_KEYWORDS:
        for keyword in keywords:
            if keyword in lowered:
                return kind
    return None


def classify_failure(exc: BaseException | None) -> str:
    """把异常归因为 FailureKind。

    归因顺序：异常类型 -> 状态码 -> 文案。类型最可靠（TimeoutError 不会误判），
    文案最不可靠但覆盖面最广，因此放在最后作为兜底。
    """
    if exc is None:
        return FailureKind.UNKNOWN

    by_name = _exception_name_kind(exc)
    if by_name:
        return by_name

    status = _status_code_of(exc)
    if status is not None:
        mapped = _STATUS_CODE_KINDS.get(status)
        if mapped:
            return mapped
        if 400 <= status < 500:
            # 未登记的 4xx：再看文案能否细分（模型不存在 / 上下文超长等）。
            return _message_kind(f"{exc}") or FailureKind.UNKNOWN
        if status >= 500:
            return FailureKind.NETWORK

    return _message_kind(f"{exc}") or FailureKind.UNKNOWN


def resolve_policy(
    kind: str, *, fallback_on_content_refusal: bool = False
) -> FailurePolicy:
    """取失败原因对应的策略，并按用户配置放开内容拒绝的回退。"""
    if kind == FailureKind.CONTENT_REFUSAL and fallback_on_content_refusal:
        return _CONTENT_REFUSAL_FALLBACK
    return FAILURE_POLICY.get(kind, FAILURE_POLICY[FailureKind.UNKNOWN])


__all__ = [
    "FAILURE_POLICY",
    "FailureKind",
    "FailurePolicy",
    "classify_failure",
    "resolve_policy",
]
