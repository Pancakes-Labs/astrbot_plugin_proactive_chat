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
    # 是否执行了文本分段。
    segmented: bool = False
    # 实际发送的批次数量（媒体锚点也会产生额外批次）。
    segment_count: int = 0
    # 文本段数量，用于区分“分段产生”与“媒体锚点产生”的多批次。
    text_segment_count: int = 0
    # 装饰钩子是否终止了发送。
    stopped_by_hook: bool = False
    # 成功送达的媒体计数（类型 -> 数量）。
    media_sent: dict[str, int] = field(default_factory=dict)
    # 因平台不支持而降级为文本占位的媒体数量。
    media_degraded: int = 0
    # 被策略剥离的组件描述列表。
    dropped_components: list[dict[str, Any]] = field(default_factory=list)
    # 是否因链中已有语音而跳过 TTS。
    tts_skipped: bool = False
    # 扩展信息。
    metadata: dict[str, Any] = field(default_factory=dict)
