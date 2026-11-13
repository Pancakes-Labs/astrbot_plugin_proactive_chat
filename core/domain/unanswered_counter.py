"""未回复计数的读取语义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UNANSWERED_COUNT_KEY = "unanswered_count"

# 损坏残余的判定标记：
#   last_scheduled_at 存在，但 next_trigger_time 不存在。
# 正常流程中两者总是成对写入（schedule_next_chat_and_save 同时写两者），
# 只有「任务已登记、却没能走完发送与收尾」才会只剩前者。
# 注意不能把 next_trigger_time 单独当痕迹：沉默计时器触发、免打扰重调度、
# 管理端重新排期都会以 reset_counter=False 只写 next_trigger_time 与
# last_scheduled_at 而不写计数，那属于正常流程，不能误判成数据丢失。
_TRACE_KEY = "last_scheduled_at"
_PAIR_KEY = "next_trigger_time"

# 计数缺失原因。
REASON_RECORDED = "recorded"  # 字段存在且可解析
REASON_NO_SESSION_DATA = "no_session_data"  # 会话数据整条不存在（全新会话）
REASON_COUNT_MISSING = "count_missing"  # 记录存在但无计数，也无残缺痕迹
REASON_MISSING_WITH_HISTORY = "count_missing_with_history"  # 残缺调度残余但缺计数


@dataclass(frozen=True)
class UnansweredState:
    """一次未回复计数读取的完整结果。"""

    count: int
    known: bool
    reason: str

    @property
    def is_trustworthy(self) -> bool:
        """计数是否可作为上限判定的依据。

        只有 count_missing_with_history 属于「登记过任务但计数丢了」，
        其余缺失场景都可以安全地视为从未触发过主动消息。
        """
        if self.known:
            return True
        return self.reason != REASON_MISSING_WITH_HISTORY


def resolve_unanswered_state(session_info: Any) -> UnansweredState:
    """解析会话记录中的未回复计数状态。"""
    if not isinstance(session_info, dict):
        return UnansweredState(0, False, REASON_NO_SESSION_DATA)

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
