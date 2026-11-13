"""未回复计数的读取语义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UNANSWERED_COUNT_KEY = "unanswered_count"

# 显式「计数不可信」标记。
# 一旦通过痕迹推断出计数丢失，调用方应立即落盘这个标记：
# 后续重新排期会补写 next_trigger_time，让痕迹判定失效，
# 只有持久标记才能继续拦住该会话，直到用户真实回复。
UNTRUSTED_MARK_KEY = "unanswered_count_untrusted"

# 残缺残余的判定标记：
#   last_scheduled_at 存在，但 next_trigger_time 不存在，且没有计数。
# 正常的首次自动触发不会命中：该路径现在会初始化 unanswered_count。
_TRACE_KEY = "last_scheduled_at"
_PAIR_KEY = "next_trigger_time"

# 计数缺失原因。
REASON_RECORDED = "recorded"  # 字段存在且可解析
REASON_NO_SESSION_DATA = "no_session_data"  # 会话数据整条不存在（全新会话）
REASON_COUNT_MISSING = "count_missing"  # 记录存在但无计数，也无残缺痕迹
REASON_MISSING_WITH_HISTORY = "count_missing_with_history"  # 计数丢失，不可信


@dataclass(frozen=True)
class UnansweredState:
    """一次未回复计数读取的完整结果。"""

    count: int
    known: bool
    reason: str

    @property
    def is_trustworthy(self) -> bool:
        """计数是否可作为上限判定的依据。

        只有 count_missing_with_history 属于「计数已丢失」，
        其余缺失场景都可以安全地视为从未触发过主动消息。
        """
        if self.known:
            return True
        return self.reason != REASON_MISSING_WITH_HISTORY


def resolve_unanswered_state(session_info: Any) -> UnansweredState:
    """解析会话记录中的未回复计数状态。

    优先级：显式不可信标记 > 真实计数字段 > 残缺痕迹推断。
    """
    if not isinstance(session_info, dict):
        return UnansweredState(0, False, REASON_NO_SESSION_DATA)

    # 显式标记优先级最高：即使后续被补写了计数，也不应自动恢复信任。
    # 正常流程会在用户真实回复时清除该标记。
    if session_info.get(UNTRUSTED_MARK_KEY):
        return UnansweredState(0, False, REASON_MISSING_WITH_HISTORY)

    raw = session_info.get(UNANSWERED_COUNT_KEY)
    if raw is not None:
        try:
            return UnansweredState(max(0, int(raw)), True, REASON_RECORDED)
        except (TypeError, ValueError):
            # 值损坏按缺失处理，继续走痕迹判定。
            pass

    if _TRACE_KEY in session_info and _PAIR_KEY not in session_info:
        return UnansweredState(0, False, REASON_MISSING_WITH_HISTORY)
    return UnansweredState(0, False, REASON_COUNT_MISSING)


def mark_untrusted(session_info: Any) -> bool:
    """把会话记录标记为「计数不可信」，返回是否发生了变更。

    只应在推断出计数丢失时调用；调用方负责落盘。
    """
    if not isinstance(session_info, dict):
        return False
    if session_info.get(UNTRUSTED_MARK_KEY):
        return False
    session_info[UNTRUSTED_MARK_KEY] = True
    return True


def clear_untrusted(session_info: Any) -> bool:
    """清除「计数不可信」标记，返回是否发生了变更。

    用户真实回复或人工重置计数后调用：此时计数重新有意义。
    """
    if not isinstance(session_info, dict):
        return False
    return session_info.pop(UNTRUSTED_MARK_KEY, None) is not None


def reaches_max_unanswered(state: UnansweredState, max_unanswered: Any) -> bool:
    """计数是否已达到配置的上限。

    上限小于等于 0 表示不限制，永远返回 False。
    计数不可信时返回 False，由调用方另行决定是否保守跳过。
    """
    try:
        limit = int(max_unanswered)
    except (TypeError, ValueError):
        return False
    if limit <= 0:
        return False
    return state.known and state.count >= limit


def is_unanswered_limit_enabled(max_unanswered: Any) -> bool:
    """上限配置是否真正启用（大于 0）。"""
    try:
        return int(max_unanswered) > 0
    except (TypeError, ValueError):
        return False
