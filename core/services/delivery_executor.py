"""投递执行服务。

消费 DeliveryPlan，按批次逐条发送，并负责：
TTS 发送与顺序控制、媒体发送失败降级、间隔计算与统计回填。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain

from ..domain.delivery_plan import (
    BATCH_MEDIA,
    DeliveryBatch,
    DeliveryPlan,
    degraded_components,
)
from ..domain.media_policy import is_text_component, text_of
from ..domain.tool_markup_cleaner import clean_llm_text
from .delivery_planner import TTS_ORDER_TEXT_FIRST


class DeliveryExecutor:
    """按投递计划执行发送。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def execute_plan(
        self,
        *,
        session_id: str,
        plan: DeliveryPlan,
        event: Any,
        report: Any,
        sender_service: Any,
        settings: dict[str, Any],
    ) -> bool:
        """执行计划，返回是否有任何内容成功送达。"""
        session_config = self.plugin.config_service.get_session_config(session_id) or {}
        seg_conf = session_config.get("segmented_reply_settings") or {}

        if plan.tts_skipped_existing_record:
            logger.debug("[主动消息] 消息链中已含语音，跳过 TTS 合成喵。")

        tts_on = bool(plan.tts_requested)
        always_text = bool(plan.tts_always_send_text)
        text_first = settings["tts_order"] == TTS_ORDER_TEXT_FIRST
        # 语音承载正文时，批次里要剔掉文本，否则用户听到一遍又看到一遍。
        withhold_text = tts_on and not always_text

        voice_sent = False
        any_sent = False
        sent_batches = 0
        media_sent: dict[str, int] = {}
        degraded_total = 0
        # 收集真正送达的文本批次，供报告回填后只归档用户看到的内容。
        delivered_text: list[str] = []
        # 正文是否已由文本批次送达：与 delivered_text 解耦
        body_text_sent = [False]

        if text_first:
            # 图文优先：先发批次，再补语音。
            batch_plan = plan.media_only() if withhold_text else plan
            sent, batches, media, degraded = await self._send_batches(
                plan=batch_plan,
                session_id=session_id,
                event=event,
                sender_service=sender_service,
                settings=settings,
                seg_conf=seg_conf,
                delivered_text=delivered_text,
                body_text_sent=body_text_sent,
            )
            any_sent = any_sent or sent
            sent_batches += batches
            degraded_total += degraded
            self._merge_counts(media_sent, media)

            if tts_on:
                voice_sent = await self._send_tts(
                    session_id=session_id,
                    plan=plan,
                    event=event,
                    sender_service=sender_service,
                )
                any_sent = any_sent or voice_sent
                if voice_sent:
                    await asyncio.sleep(0.5)
                elif withhold_text:
                    # 语音失败且正文已被扣下：补发文本，保证有正文送达。
                    # 只发文本批：媒体批已在上面发过，整份重发会重复。
                    logger.warning("[主动消息] 语音合成失败，补发被扣下的文本批次喵。")
                    sent2, batches2, media2, degraded2 = await self._send_batches(
                        plan=plan.text_only(),
                        session_id=session_id,
                        event=event,
                        sender_service=sender_service,
                        settings=settings,
                        seg_conf=seg_conf,
                        delivered_text=delivered_text,
                        body_text_sent=body_text_sent,
                    )
                    any_sent = any_sent or sent2
                    sent_batches += batches2
                    degraded_total += degraded2
                    self._merge_counts(media_sent, media2)
        else:
            # 语音优先：先发语音，再发图文。
            if tts_on:
                voice_sent = await self._send_tts(
                    session_id=session_id,
                    plan=plan,
                    event=event,
                    sender_service=sender_service,
                )
                any_sent = any_sent or voice_sent
                if voice_sent:
                    await asyncio.sleep(0.5)

            # 语音成功且不附带原文时才裁剪文本；语音失败仍发完整内容兜底。
            batch_plan = plan.media_only() if (voice_sent and not always_text) else plan
            if voice_sent and not always_text:
                logger.info(
                    "[主动消息] 语音已送达且未开启附带原文，本次仅补发媒体批次喵。"
                )
            sent, batches, media, degraded = await self._send_batches(
                plan=batch_plan,
                session_id=session_id,
                event=event,
                sender_service=sender_service,
                settings=settings,
                seg_conf=seg_conf,
                delivered_text=delivered_text,
                body_text_sent=body_text_sent,
            )
            any_sent = any_sent or sent
            sent_batches += batches
            degraded_total += degraded
            self._merge_counts(media_sent, media)

        if report is not None:
            # 语音已送达但正文从未由文本批次送达：正文由语音承载，必须入档。
            archive_parts: list[str] = [item for item in delivered_text if item.strip()]
            if voice_sent and not body_text_sent[0]:
                voice_body = plan.visible_text.strip()
                if voice_body:
                    # 语音优先时正文在前，图文优先时正文在后，保持时间顺序。
                    if text_first:
                        archive_parts.append(voice_body)
                    else:
                        archive_parts.insert(0, voice_body)
            report.delivered_text = "\n\n".join(archive_parts)
            report.voice_sent = bool(voice_sent)
            report.segmented = bool(plan.segmented)
            report.segment_count = sent_batches
            report.text_segment_count = plan.text_segment_total
            report.media_sent = media_sent
            report.media_degraded = degraded_total
            report.dropped_components = list(plan.dropped)
            report.tts_skipped = bool(plan.tts_skipped_existing_record)
            if plan.warnings:
                report.metadata.setdefault("plan_warnings", list(plan.warnings))

        return any_sent

    async def _send_batches(
        self,
        *,
        plan: DeliveryPlan,
        session_id: str,
        event: Any,
        sender_service: Any,
        settings: dict[str, Any],
        seg_conf: dict,
        delivered_text: list[str],
        body_text_sent: list[bool],
    ) -> tuple[bool, int, dict[str, int], int]:
        """逐批发送，返回 (是否送出, 批次数, 媒体计数, 降级数)。

        delivered_text 为外部传入的收集器：只有真正送达的文本批次才会写入，
        供 RECORD 阶段只归档用户确实看到的内容。
        body_text_sent 为单元素列表，用于标记正文是否由文本批次送达。
        """
        any_sent = False
        sent_batches = 0
        media_sent: dict[str, int] = {}
        degraded_total = 0

        for index, batch in enumerate(plan.batches):
            ok, degraded = await self._send_batch(
                session_id=session_id,
                batch=batch,
                event=event,
                sender_service=sender_service,
                degrade_on_failure=settings["degrade_on_failure"],
                meta=plan.chain_meta,
                timeout=float(settings.get("send_timeout_seconds") or 0),
            )
            if ok:
                any_sent = True
                sent_batches += 1
                degraded_total += degraded
                self._collect_delivered_text(
                    batch,
                    delivered_text,
                    degraded=bool(degraded),
                    body_text_sent=body_text_sent,
                )
                # 降级成功只说明占位文本送达，媒体本身并未送达，
                # 因此不计入媒体统计，避免历史里出现虚假的送达记录。
                if not degraded:
                    self._merge_counts(media_sent, batch.media_counts())

            if index < len(plan.batches) - 1:
                await asyncio.sleep(self._interval_for(batch, seg_conf, settings))

        return any_sent, sent_batches, media_sent, degraded_total

    @staticmethod
    def _collect_delivered_text(
        batch: DeliveryBatch,
        sink: list[str],
        *,
        degraded: bool = False,
        body_text_sent: list[bool] | None = None,
    ) -> None:
        """收集已送达批次的文本。

        降级批次实际发出的是媒体占位文本，因此按占位符收集，
        不能把被替换掉的媒体记成送达，也不能让纯媒体降级后历史留空。
        body_text_sent 只标记非降级的真实正文是否送达，供语音归档判断。
        """
        if degraded:
            items, _ = degraded_components(batch.components)
            for item in items:
                if isinstance(item, str) and item.strip():
                    sink.append(item)
                elif is_text_component(item):
                    text = text_of(item)
                    if text.strip():
                        sink.append(text)
            return

        for comp in batch.components:
            if is_text_component(comp):
                text = text_of(comp)
                if text.strip():
                    sink.append(text)
                    if body_text_sent is not None:
                        body_text_sent[0] = True

    @staticmethod
    def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
        for media_type, count in source.items():
            target[media_type] = target.get(media_type, 0) + count

    # ------------------------------------------------------------------
    # 单批发送
    # ------------------------------------------------------------------
    async def _send_batch(
        self,
        *,
        session_id: str,
        batch: DeliveryBatch,
        event: Any,
        sender_service: Any,
        degrade_on_failure: bool,
        meta: dict | None = None,
        timeout: float = 0.0,
    ) -> tuple[bool, int]:
        """发送一个批次，失败时按需降级为文本占位重试一次。"""
        if not batch.components:
            return False, 0

        components = self._build_chain(batch)
        if await self._send_once(
            sender_service, session_id, event, components, meta, timeout
        ):
            return True, 0

        if not degrade_on_failure or not batch.has_media():
            return False, 0

        fallback, replaced = self._degrade_batch(batch)
        if not fallback:
            return False, 0

        logger.warning(
            f"[主动消息] {batch.kind} 批次发送失败，已将 {replaced} 个媒体降级为文本占位重试喵。"
        )
        if await self._send_once(
            sender_service, session_id, event, fallback, meta, timeout
        ):
            batch.degraded = True
            return True, replaced
        return False, 0

    @staticmethod
    async def _send_once(
        sender_service: Any,
        session_id: str,
        event: Any,
        components: list,
        meta: dict | None,
        timeout: float,
    ) -> bool:
        """执行一次发送；超时按失败处理，避免平台卡死拖住后续批次。"""
        call = sender_service.send_chain(session_id, event, components, meta)
        if timeout <= 0:
            return (await call) is True
        try:
            return (await asyncio.wait_for(call, timeout=timeout)) is True
        except asyncio.TimeoutError:
            logger.warning(
                f"[主动消息] 单批发送超过 {timeout:.0f} 秒仍未返回，按发送失败处理喵。"
            )
            return False

    @staticmethod
    def _build_chain(batch: DeliveryBatch) -> list:
        """批次组件转成可直接交给 send_chain 的列表。"""
        return [comp for comp in batch.components if comp is not None]

    @staticmethod
    def _degrade_batch(batch: DeliveryBatch) -> tuple[list, int]:
        """把批次内的媒体替换为文本占位，保持原有阅读顺序。"""
        items, replaced = degraded_components(batch.components)
        result: list = []
        for item in items:
            if isinstance(item, str):
                if item.strip():
                    result.append(Plain(text=item))
                continue
            result.append(item)
        return result, replaced

    @staticmethod
    def _interval_for(
        batch: DeliveryBatch,
        seg_conf: dict,
        settings: dict[str, Any],
    ) -> float:
        """计算批次之间的等待时间。

        纯媒体批没有正文，长度对数会退化成 0~0.5 秒抖动，
        因此改用固定间隔。
        """
        if batch.kind == BATCH_MEDIA or not batch.text_length:
            return float(settings["media_interval_seconds"])

        interval_method = seg_conf.get("interval_method", "random")
        if interval_method == "log":
            import math
            import random

            try:
                log_base = float(seg_conf.get("log_base", 1.8))
            except (TypeError, ValueError):
                log_base = 1.8
            if log_base <= 1:
                log_base = 1.8
            i = math.log(batch.text_length + 1, log_base)
            return random.uniform(i, i + 0.5)

        import random

        interval_str = seg_conf.get("interval", "1.5, 3.5")
        try:
            interval_ls = [
                float(item) for item in str(interval_str).replace(" ", "").split(",")
            ]
            interval = interval_ls if len(interval_ls) == 2 else [1.5, 3.5]
        except Exception:  # noqa: BLE001 - 配置非法时回退默认区间
            interval = [1.5, 3.5]
        return random.uniform(interval[0], interval[1])

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------
    async def _send_tts(
        self,
        *,
        session_id: str,
        plan: DeliveryPlan,
        event: Any,
        sender_service: Any,
    ) -> bool:
        """合成并发送语音，返回是否成功送达。"""
        text = clean_llm_text(plan.visible_text)
        if not text.strip():
            return False

        plugin = self.plugin
        try:
            logger.info("[主动消息] 尝试进行手动TTS喵。")
            tts_provider = plugin.context.get_using_tts_provider(umo=session_id)
            if not tts_provider:
                return False
            audio_path = await tts_provider.get_audio(text)
            if not audio_path:
                return False
            sent = await sender_service.send_chain(
                session_id, event, [Record(file=audio_path)]
            )
            return sent is True
        except Exception as e:
            logger.error(f"[主动消息] 手动TTS流程发生异常喵: {e}")
            if plugin.telemetry and plugin.telemetry.enabled:
                plugin._track_task(
                    asyncio.create_task(
                        plugin.telemetry.track_error(
                            e,
                            module="core.services.delivery_executor._send_tts",
                        )
                    )
                )
            return False


def build_component_chain(components: list, meta: dict[str, Any]) -> MessageChain:
    """构造消息链并保留装饰钩子设置的链级元数据。

    直接用 MessageChain(list) 会丢掉 use_t2i_ / use_markdown_ / type，
    导致装饰钩子表达的 markdown 意图在 QQ Official 等平台失效。
    """
    chain = MessageChain(list(components))
    for attr in ("use_t2i_", "use_markdown_", "type"):
        if attr in meta:
            try:
                setattr(chain, attr, meta[attr])
            except Exception:  # pragma: no cover - 旧版本缺少该字段
                continue
    return chain


__all__ = ["DeliveryExecutor", "build_component_chain"]
