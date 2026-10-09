"""流水线上文构建辅助。

负责把一次触发（会话标识 + 触发来源）转换为携带配置与运行态的
ProactiveContext，供各 Stage 消费。
"""

from __future__ import annotations

import time

from ..domain.domain_enums import TriggerSource
from ..domain.request_context import ProactiveContext
from ..domain.session_config import SessionConfig
from ..domain.session_key import SessionKey


def build_context(
    session_id: str,
    session: SessionKey,
    trigger: TriggerSource,
    config: dict | None,
    unanswered_count: int = 0,
) -> ProactiveContext:
    """构建初始 ProactiveContext。"""
    ctx = ProactiveContext()
    ctx.session_id = session_id
    ctx.session = session
    ctx.trigger = trigger
    ctx.config = SessionConfig.from_dict(config)
    ctx.unanswered_count = unanswered_count
    ctx.started_at = time.time()
    return ctx
