"""投递计划：把一条装饰后的消息链整理成可逐条发送的批次。

主动消息的媒体混排顺序在平台侧无法精细控制，因此这里把“链”翻译成
“批次序列”：文本段各自成批，媒体组件作为锚点跟随前一文本批。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .media_policy import (
    is_media_component,
    is_text_component,
    media_placeholder,
    media_type_of,
    text_of,
)

# 批次形态
BATCH_TEXT = "text"
BATCH_TEXT_MEDIA = "text_media"
BATCH_MEDIA = "media"

# 媒体分组策略
GROUPING_FOLLOW_TEXT = "follow_text"
GROUPING_OWN_MESSAGE = "own_message"

# 事件序列标记
_EVENT_TEXT = "text"
_EVENT_MEDIA = "media"

_MEDIA_UNITS = {
    "image": "张图片",
    "record": "条语音",
    "video": "个视频",
    "file": "个文件",
    "json": "张卡片",
}


@dataclass
class DeliveryBatch:
    """一个发送批次：一次 send_chain 调用对应的组件集合。"""

    components: list = field(default_factory=list)
    kind: str = BATCH_TEXT
    text_length: int = 0
    degraded: bool = False

    # ------------------------------------------------------------------
    def has_text(self) -> bool:
        return any(is_text_component(comp) for comp in self.components)

    def has_media(self) -> bool:
        return any(is_media_component(comp) for comp in self.components)

    def media_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for comp in self.components:
            if not is_media_component(comp):
                continue
            media_type = media_type_of(comp) or "unknown"
            counts[media_type] = counts.get(media_type, 0) + 1
        return counts

    def media_total(self) -> int:
        return sum(self.media_counts().values())

    def strip_text(self) -> DeliveryBatch | None:
        """去掉文本组件，仅保留媒体；没有媒体时返回 None。"""
        media = [comp for comp in self.components if is_media_component(comp)]
        if not media:
            return None
        return DeliveryBatch(
            components=media,
            kind=BATCH_MEDIA,
            text_length=0,
            degraded=self.degraded,
        )


@dataclass
class DeliveryPlan:
    """一次主动消息的完整投递计划。"""

    batches: list = field(default_factory=list)
    # 媒体统计（按类型）
    media_counts: dict[str, int] = field(default_factory=dict)
    # 被剥离的组件：[{component, type, reason, name?}]
    dropped: list = field(default_factory=list)
    # 装饰钩子返回的链元数据（use_t2i_ / use_markdown_ / type）
    chain_meta: dict = field(default_factory=dict)
    # 是否发生文本分段
    segmented: bool = False
    # 文本段总数（用于区分“分段产生”与“媒体锚点产生”的多批次）
    text_segment_total: int = 0
    # 计划内可见文本（TTS 源文本）
    visible_text: str = ""
    # 是否请求 TTS（配置开启 + 有文本 + 链中没有现成语音）
    tts_requested: bool = False
    # TTS 之后是否仍发送文本（对应 always_send_text）
    tts_always_send_text: bool = True
    # TTS 与图文的发送顺序：voice_first / text_first
    tts_order: str = "voice_first"
    # 链中已存在 Record，TTS 主动跳过
    tts_skipped_existing_record: bool = False
    # 中断原因：
    # decorated_chain_empty 钩子清空结果（本轮不发，视为成功）
    # stopped_by_hook       钩子终止传播（本轮不发）
    # no_sendable_component 组件全被策略剥离（本轮无内容可发）
    blocked_reason: str = ""
    # 规划过程中的告警，供日志与遥测
    warnings: list = field(default_factory=list)
    # 是否启用了富媒体能力
    media_enabled: bool = True

    # ------------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        """没有任何可发送内容（不含 TTS）。"""
        return not self.batches

    @property
    def media_total(self) -> int:
        return sum(self.media_counts.values())

    def media_summary(self) -> str:
        """生成人类可读的媒体摘要，用于对话历史占位与报告。"""
        return format_media_summary(self.media_counts)

    def _derive(self, batches: list, *, text_segment_total: int) -> DeliveryPlan:
        """基于当前计划派生一个批次子集，保留元数据但清空 TTS 状态。"""
        return DeliveryPlan(
            batches=batches,
            media_counts=dict(self.media_counts),
            dropped=list(self.dropped),
            chain_meta=dict(self.chain_meta),
            segmented=self.segmented,
            text_segment_total=text_segment_total,
            visible_text="",
            tts_requested=False,
            tts_always_send_text=self.tts_always_send_text,
            tts_order=self.tts_order,
            blocked_reason=self.blocked_reason,
            warnings=list(self.warnings),
            media_enabled=self.media_enabled,
        )

    def media_only(self) -> DeliveryPlan:
        """返回只保留媒体批次的新计划（TTS 成功且不再发文本时使用）。"""
        batches: list = []
        for batch in self.batches:
            stripped = batch.strip_text()
            if stripped is not None:
                batches.append(stripped)
        return self._derive(batches, text_segment_total=0)

    def text_only(self) -> DeliveryPlan:
        """返回只保留文本批次的新计划（语音失败后补发正文时使用）。"""
        batches: list = []
        for batch in self.batches:
            text_components = [
                comp for comp in batch.components if is_text_component(comp)
            ]
            if not text_components:
                continue
            batches.append(
                DeliveryBatch(
                    components=text_components,
                    kind=BATCH_TEXT,
                    text_length=batch.text_length,
                )
            )
        return self._derive(batches, text_segment_total=self.text_segment_total)


def format_media_summary(counts: dict[str, int]) -> str:
    """把媒体计数渲染成“2 张图片、1 个文件”这样的文本。"""
    if not counts:
        return ""
    parts: list[str] = []
    for media_type, count in counts.items():
        if count <= 0:
            continue
        unit = _MEDIA_UNITS.get(media_type, "个媒体")
        parts.append(f"{count} {unit}")
    return "、".join(parts)


def count_media(components: list) -> dict[str, int]:
    """统计组件列表中的媒体数量。"""
    counts: dict[str, int] = {}
    for comp in components:
        if not is_media_component(comp):
            continue
        media_type = media_type_of(comp) or "unknown"
        counts[media_type] = counts.get(media_type, 0) + 1
    return counts


def group_into_batches(
    events: list,
    *,
    media_grouping: str = GROUPING_FOLLOW_TEXT,
    max_media_per_batch: int = 4,
    text_factory=None,
) -> list:
    """把事件序列组装成发送批次。

    Args:
        events: [(kind, payload)]，kind 为 "text"（payload 是文本串）
            或 "media"（payload 是组件）。
        media_grouping: follow_text 让媒体跟随前一文本批；own_message 让
            媒体独占批次（连续同类媒体仍合并到上限）。
        max_media_per_batch: 单个批次允许携带的媒体数，<=0 表示不限制。
        text_factory: 把文本串转成消息组件的可调用对象。domain 层不导入
            AstrBot，因此必须由调用方注入；缺失时会把裸字符串放进批次，
            调用方需自行兜底。

    Returns:
        批次列表；空输入返回空列表。
    """
    limit = max_media_per_batch if max_media_per_batch and max_media_per_batch > 0 else 0
    batches: list = []
    current: list = []
    current_text_len = 0
    current_media = 0

    def flush() -> None:
        nonlocal current, current_text_len, current_media
        if not current:
            return
        batch = DeliveryBatch(
            components=current,
            text_length=current_text_len,
        )
        if batch.has_text() and batch.has_media():
            batch.kind = BATCH_TEXT_MEDIA
        elif batch.has_media():
            batch.kind = BATCH_MEDIA
        else:
            batch.kind = BATCH_TEXT
        batches.append(batch)
        current = []
        current_text_len = 0
        current_media = 0

    for kind, payload in events:
        if kind == _EVENT_TEXT:
            text = payload if isinstance(payload, str) else ""
            if not text.strip():
                continue
            # 每个文本段独立成批：批边界即用户看到的“一句一条”。
            flush()
            current_text_len = len(text)
            current.append(text_factory(text) if text_factory else text)
            continue

        # 媒体事件
        if media_grouping == GROUPING_OWN_MESSAGE:
            # limit 为 0 表示不限制：连续媒体合并，不逐条拆分。
            if limit and current_media >= limit:
                flush()
            elif current_text_len:
                # own_message 下媒体不与文本同批，先收掉当前文本批。
                flush()
            current.append(payload)
            current_media += 1
            continue

        # follow_text：媒体跟随当前批
        if limit and current_media >= limit:
            flush()
        current.append(payload)
        current_media += 1

    flush()
    return batches


def build_event_sequence(
    components: list,
    *,
    splitter=None,
    allow_split: bool = False,
) -> tuple[list, int]:
    """把组件列表展开成文本/媒体事件序列。

    Args:
        components: 已清洗、已剥离交互组件的组件列表。
        splitter: 可调用对象，接收文本返回文本段列表；allow_split 为
            False 时不会被调用。
        allow_split: 是否允许对文本做分段。

    Returns:
        (事件序列, 文本段总数)。文本段总数用于判断是否真的分过段。
    """
    events: list = []
    text_segments = 0
    for comp in components:
        if is_text_component(comp):
            text = text_of(comp)
            if not text.strip():
                continue
            segments = [text]
            if allow_split and splitter is not None:
                produced = splitter(text)
                if produced:
                    segments = produced
            for segment in segments:
                if not str(segment).strip():
                    continue
                events.append((_EVENT_TEXT, segment))
                text_segments += 1
            continue

        if is_media_component(comp):
            events.append((_EVENT_MEDIA, comp))
    return events, text_segments


def degraded_components(components: list) -> tuple[list, int]:
    """把批次内的媒体组件替换成文本占位。

    返回 (条目列表, 被替换的媒体数)；条目里的 str 需由调用方转成
    文本组件（domain 层不导入 AstrBot 组件）。

    文本次序保持原样：图片夹在两段文字之间时，占位符会落在同一位置，
    保证降级后阅读顺序仍然正确。
    """
    items: list = []
    replaced = 0
    for comp in components:
        if is_media_component(comp):
            items.append(media_placeholder(comp))
            replaced += 1
            continue
        items.append(comp)
    return items, replaced


__all__ = [
    "BATCH_MEDIA",
    "BATCH_TEXT",
    "BATCH_TEXT_MEDIA",
    "DeliveryBatch",
    "DeliveryPlan",
    "GROUPING_FOLLOW_TEXT",
    "GROUPING_OWN_MESSAGE",
    "build_event_sequence",
    "count_media",
    "degraded_components",
    "format_media_summary",
    "group_into_batches",
]
