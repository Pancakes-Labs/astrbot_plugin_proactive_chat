"""发送结果数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeliveryReport:
    """一次主动消息发送的汇总结果。"""

    sent: bool = False
    # 是否成功发出 TTS 语音。
    voice_sent: bool = False
    # 是否执行了分段。
    segmented: bool = False
    # 实际发送的分段数量。
    segment_count: int = 0
    # 装饰钩子是否终止了发送。
    stopped_by_hook: bool = False
    # 扩展信息。
    metadata: dict[str, Any] = field(default_factory=dict)
