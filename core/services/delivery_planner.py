"""投递计划构建服务。

把 DELIBERATE 产物整理成可安全发送的批次序列，集中承担：
装饰钩子、类型化清洗、交互组件剥离、媒体来源校验、文本分段、
媒体锚点分组与 TTS 判定。发送动作本身由 DeliveryExecutor 负责。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Plain

from ..domain.config_keys import RICH_MEDIA_SETTINGS_KEY
from ..domain.delivery_plan import (
    GROUPING_FOLLOW_TEXT,
    GROUPING_OWN_MESSAGE,
    DeliveryBatch,
    DeliveryPlan,
    build_event_sequence,
    count_media,
    group_into_batches,
)
from ..domain.media_policy import (
    KIND_INTERACTIVE,
    component_class_name,
    component_kind,
    describe_component,
    is_media_component,
    is_safe_media_source,
    is_text_component,
    media_type_of,
    text_of,
)
from ..domain.tool_markup_cleaner import clean_chain_components

# TTS 与图文的发送顺序
TTS_ORDER_VOICE_FIRST = "voice_first"
TTS_ORDER_TEXT_FIRST = "text_first"

# 未知组件处理策略
UNKNOWN_DROP = "drop"
UNKNOWN_PLACEHOLDER = "placeholder"

DEFAULT_MAX_MEDIA_PER_BATCH = 4
DEFAULT_MEDIA_INTERVAL_SECONDS = 1.0
DEFAULT_MEDIA_TIMEOUT_SECONDS = 30.0


class DeliveryPlanner:
    """构建一次主动消息的投递计划。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    def get_settings(self, session_config: dict) -> dict[str, Any]:
        """读取并归一化富媒体配置。"""
        raw = {}
        if isinstance(session_config, dict):
            value = session_config.get(RICH_MEDIA_SETTINGS_KEY)
            if isinstance(value, dict):
                raw = value

        grouping = str(raw.get("media_grouping") or GROUPING_FOLLOW_TEXT)
        if grouping not in (GROUPING_FOLLOW_TEXT, GROUPING_OWN_MESSAGE):
            grouping = GROUPING_FOLLOW_TEXT

        unknown_policy = str(raw.get("unknown_component_policy") or UNKNOWN_DROP)
        if unknown_policy not in (UNKNOWN_DROP, UNKNOWN_PLACEHOLDER):
            unknown_policy = UNKNOWN_DROP

        tts_order = str(raw.get("tts_order") or TTS_ORDER_VOICE_FIRST)
        if tts_order not in (TTS_ORDER_VOICE_FIRST, TTS_ORDER_TEXT_FIRST):
            tts_order = TTS_ORDER_VOICE_FIRST

        return {
            "enabled": self._as_bool(raw.get("enabled"), True),
            "media_grouping": grouping,
            "unknown_component_policy": unknown_policy,
            "strip_interactive_components": self._as_bool(
                raw.get("strip_interactive_components"), True
            ),
            "degrade_on_failure": self._as_bool(raw.get("degrade_on_failure"), True),
            "max_media_per_batch": self._as_int(
                raw.get("max_media_per_batch"), DEFAULT_MAX_MEDIA_PER_BATCH, 0
            ),
            "media_interval_seconds": self._as_float(
                raw.get("media_interval_seconds"), DEFAULT_MEDIA_INTERVAL_SECONDS
            ),
            "media_timeout_seconds": self._as_float(
                raw.get("media_timeout_seconds"), DEFAULT_MEDIA_TIMEOUT_SECONDS, 1.0
            ),
            "restrict_local_media": self._as_bool(
                raw.get("restrict_local_media"), True
            ),
            "tts_order": tts_order,
        }

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on"}:
                return True
            if normalized in {"false", "0", "no", "n", "off", ""}:
                return False
        return default

    @staticmethod
    def _as_int(value: Any, default: int, minimum: int = 0) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(parsed, minimum)

    @staticmethod
    def _as_float(value: Any, default: float, minimum: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return max(parsed, minimum)

    # ------------------------------------------------------------------
    # 媒体来源白名单
    # ------------------------------------------------------------------
    def allowed_media_dirs(self) -> list[str]:
        """返回允许发送的本地媒体根目录。"""
        dirs: list[str] = []
        data_dir = getattr(self.plugin, "data_dir", None)
        if data_dir:
            dirs.append(str(data_dir))

        try:  # 取决于 AstrBot 版本
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            dirs.append(str(get_astrbot_temp_path()))
        except Exception:  # pragma: no cover - 旧版本缺少该工具
            pass

        return dirs

    # ------------------------------------------------------------------
    # 组件规整
    # ------------------------------------------------------------------
    def assemble_base_components(self, text: str, initial_chain: list | None) -> list:
        """组装发送前的组件列表：链里没有文本时补到开头。"""
        base: list = list(initial_chain) if initial_chain else []
        text_value = (text or "").strip()
        if text_value and not any(is_text_component(comp) for comp in base):
            base.insert(0, Plain(text=text_value))
        return base

    def sanitize_components(
        self,
        components: list,
        settings: dict[str, Any],
    ) -> tuple[list, list, int]:
        """剥离交互组件、校验媒体来源。

        Returns:
            (可用组件, 真正被丢弃的描述列表, 转成占位的未知组件数)。
            转占位的组件已写进可用组件，不计入被剥离。
        """
        allowed_dirs = self.allowed_media_dirs()
        restrict_local = settings["restrict_local_media"]
        unknown_policy = settings["unknown_component_policy"]
        strip_interactive = settings["strip_interactive_components"]

        kept: list = []
        dropped: list = []
        downgraded = 0

        for comp in components:
            kind = component_kind(comp)

            if kind == KIND_INTERACTIVE:
                if strip_interactive:
                    dropped.append(
                        describe_component(comp, "interactive_component")
                    )
                else:
                    kept.append(comp)
                continue

            if is_media_component(comp):
                if restrict_local:
                    safe, reason = is_safe_media_source(comp, allowed_dirs)
                    if not safe:
                        dropped.append(describe_component(comp, reason))
                        continue
                kept.append(comp)
                continue

            if is_text_component(comp):
                kept.append(comp)
                continue

            # 其余为未知组件
            if unknown_policy == UNKNOWN_PLACEHOLDER:
                placeholder = f"[{component_class_name(comp)}]"
                kept.append(Plain(text=placeholder))
                downgraded += 1
                continue
            dropped.append(describe_component(comp, "unknown_component"))

        return kept, dropped, downgraded

    # ------------------------------------------------------------------
    # 计划构建
    # ------------------------------------------------------------------
    async def build_plan(
        self,
        *,
        session_id: str,
        session_config: dict,
        text: str,
        initial_chain: list | None,
        event: Any,
        sender_service: Any,
    ) -> DeliveryPlan:
        """构建投递计划。

        Args:
            session_id: 会话 UMO。
            session_config: 已合并的会话配置。
            text: LLM 生成文本（可能已被清洗过）。
            initial_chain: DELIBERATE 产出的消息链，可为空。
            event: 伪事件，用于派发装饰钩子。
            sender_service: 提供装饰钩子与分段能力的发送服务。
        """
        settings = self.get_settings(session_config)
        plan = DeliveryPlan(media_enabled=settings["enabled"])
        plan.tts_always_send_text = self._tts_always_send_text(session_config)
        plan.tts_order = settings["tts_order"]

        # 1) 组装并派发装饰钩子（提前到 TTS 之前，钩子拦截时不会先发语音）。
        base = self.assemble_base_components(text, initial_chain)
        decorated, should_send = await sender_service.run_decorating_hooks(
            event, base
        )
        if not should_send:
            plan.blocked_reason = "stopped_by_hook"
            return plan
        if not decorated:
            # 钩子把结果清空了：这是本次不发而非失败，调用方应静默跳过。
            plan.blocked_reason = "decorated_chain_empty"
            plan.warnings.append("decorated_chain_empty")
            return plan

        # 2) 类型化清洗：钩子可能注入带标记的文本组件。
        decorated = clean_chain_components(decorated)

        # 3) 剥离交互组件 / 校验媒体来源。
        # 返回的 dropped 仅含真正被丢弃的组件；转占位的未知组件另计入 downgraded。
        components, dropped, downgraded = self.sanitize_components(
            decorated, settings
        )
        plan.dropped = dropped
        if dropped:
            logger.info(
                f"[主动消息] 已剥离 {len(dropped)} 个不可投递组件喵："
                + "、".join(
                    f"{item.get('component')}({item.get('reason')})" for item in dropped
                )
            )
        if downgraded:
            logger.info(
                f"[主动消息] {downgraded} 个未知组件已转为文本占位喵。"
            )
        if not components:
            # 这里是因为组件全被策略剥离，属于生成内容不可投递，
            # 与钩子主动清空不同，调用方应视为本轮失败。
            plan.blocked_reason = "no_sendable_component"
            plan.warnings.append("no_sendable_component")
            return plan

        # 媒体计数与 TTS 源文本必须在降级前取：
        # 降级会把媒体换成 [图片] 这类占位，之后统计会失真，
        # 而 TTS 也不该把占位符念出来。
        plan.media_counts = count_media(components)
        # 多个 Plain 之间补空行，避免 TTS 把两段话连读成一串。
        plan.visible_text = "\n\n".join(
            text_of(comp) for comp in components if is_text_component(comp)
        ).strip()

        # 4) 媒体能力开关关闭时，媒体降级为文本占位。
        if not settings["enabled"]:
            components = self._downgrade_media(components, plan)

        # 5) 文本分段：整链纯文本总长超过阈值时不分段。
        # 组件数在降级后统计，占位符也会作为独立文本段参与判定。
        seg_conf = session_config.get("segmented_reply_settings") or {}
        allow_split, text_total = self._resolve_split(seg_conf, components)
        text_component_total = sum(
            1 for comp in components if is_text_component(comp)
        )
        if text_total:
            plan.warnings.append(f"text_length:{text_total}")

        def _splitter(value: str) -> list[str]:
            return sender_service.split_text(value, seg_conf)

        events, text_segments = build_event_sequence(
            components, splitter=_splitter, allow_split=allow_split
        )
        plan.text_segment_total = text_segments

        # 6) 组装批次；文本段之间只要不是同一个事件，就算分段生效。
        batches = group_into_batches(
            events,
            media_grouping=settings["media_grouping"],
            max_media_per_batch=settings["max_media_per_batch"],
            text_factory=lambda value: Plain(text=value),
        )
        plan.batches = [batch for batch in batches if batch.components]
        # 分段是否生效要看文本段数是否超过文本组件数：
        # 链里本来就有多个 Plain 时，段数同样大于 1，但那不是分段。
        plan.segmented = text_segments > max(text_component_total, 1)
        plan.chain_meta = self._extract_chain_meta(event)

        # 7) TTS 判定：链中已有语音时跳过，避免两条语音。
        tts_conf = session_config.get("tts_settings") or {}
        has_record = any(media_type_of(comp) == "record" for comp in components)
        plan.tts_skipped_existing_record = bool(has_record)
        plan.tts_requested = (
            bool(tts_conf.get("enabled", False))
            and bool(plan.visible_text)
            and not has_record
        )

        if not plan.batches:
            plan.warnings.append("no_batch")
        return plan

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _tts_always_send_text(session_config: dict) -> bool:
        tts_conf = session_config.get("tts_settings") or {}
        value = tts_conf.get("always_send_text", True)
        return bool(value) if isinstance(value, bool) else True

    @staticmethod
    def _extract_chain_meta(event: Any) -> dict[str, Any]:
        """读取装饰钩子结果上的链级元数据（t2i / markdown / type）。"""
        meta: dict[str, Any] = {}
        if event is None:
            return meta
        try:
            result = event.get_result()
        except Exception:  # pragma: no cover - 未知事件实现
            return meta
        if result is None:
            return meta
        for attr in ("use_t2i_", "use_markdown_", "type"):
            if hasattr(result, attr):
                meta[attr] = getattr(result, attr)
        return meta

    def _downgrade_media(self, components: list, plan: DeliveryPlan) -> list:
        """把媒体替换成文本占位（富媒体能力被关闭时）。"""
        from ..domain.delivery_plan import degraded_components

        items, replaced = degraded_components(components)
        if replaced:
            plan.warnings.append(f"media_disabled_downgraded:{replaced}")
            logger.info(
                f"[主动消息] 富媒体发送已关闭，{replaced} 个媒体组件已降级为文本占位喵。"
            )
        result: list = []
        for item in items:
            if isinstance(item, str):
                if item.strip():
                    result.append(Plain(text=item))
                continue
            result.append(item)
        return result

    @staticmethod
    def _resolve_split(seg_conf: dict, components: list) -> tuple[bool, int]:
        """判断是否允许分段，返回 (allow_split, 纯文本总长)。

        阈值语义统一为「整条回复的总字数」：多段短文本相加超过阈值时
        同样不再分段，与配置项描述保持一致。
        """
        if not seg_conf.get("enabled", False):
            return False, 0

        total = sum(
            len(text_of(comp)) for comp in components if is_text_component(comp)
        )
        try:
            threshold = int(seg_conf.get("words_count_threshold", 150))
        except (TypeError, ValueError):
            threshold = 150

        if threshold > 0 and total > threshold:
            return False, total
        return True, total


__all__ = [
    "DEFAULT_MAX_MEDIA_PER_BATCH",
    "DEFAULT_MEDIA_INTERVAL_SECONDS",
    "DEFAULT_MEDIA_TIMEOUT_SECONDS",
    "DeliveryBatch",
    "DeliveryPlanner",
    "TTS_ORDER_TEXT_FIRST",
    "TTS_ORDER_VOICE_FIRST",
    "UNKNOWN_DROP",
    "UNKNOWN_PLACEHOLDER",
]
