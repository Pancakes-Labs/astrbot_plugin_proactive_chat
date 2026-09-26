"""发送与装饰钩子模块。

本模块负责把生成的主动消息按 AstrBot 官方语义投递出去，核心原则：

1. 复用标准钩子。
2. 先装饰、后分段：官方 pipeline 是先让装饰器处理完整消息链，再执行分段。
   这里遵循同样顺序，避免装饰器只能看到文本碎片而无法解析跨段标记。
3. 单一事件贯穿，复用同一个事件实例。
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import traceback
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.platform.platform import PlatformStatus

from .proactive_event import (
    build_proactive_event_for_session,
    dispatch_event_hook,
    is_group_session,
)

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.star.star_handler import EventType
except ImportError:  # pragma: no cover
    EventType = None  # type: ignore[assignment]

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.platform.astr_message_event import MessageSession as MS
except ImportError:  # pragma: no cover
    try:
        from astrbot.core.platform.message_session import MessageSession as MS
    except ImportError:
        MS = None  # type: ignore[assignment]

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.platform.sources.webchat.message_parts_helper import (
        message_chain_to_storage_message_parts,
    )
except ImportError:  # pragma: no cover
    message_chain_to_storage_message_parts = None  # type: ignore[assignment]


class SenderMixin:
    """发送与装饰钩子混入类。"""

    context: Any
    session_data: dict
    telemetry: Any
    data_dir: Any

    # ------------------------------------------------------------------
    # 文本分段
    # ------------------------------------------------------------------
    def _split_text(self, text: str, settings: dict) -> list[str]:
        """根据配置对文本进行分段。"""
        split_mode = settings.get("split_mode", "regex")

        # 新版 AstrBot（如 v4.20.1+）中，分段正则本身不再承担“匹配后自动移除命中字符”的旧行为。
        # 因此这里显式增加一个独立的内容清理阶段：
        # 1. 先按 split_mode 执行“切段”；
        # 2. 再在每个切好的分段上按 content_cleanup_rule 做二次清理。
        # 这样可以与官方的 segmented_reply.content_cleanup_rule 机制保持一致。
        enable_content_cleanup = settings.get("enable_content_cleanup", False)
        # 只有开关开启时才启用内容过滤规则；关闭时直接置空，确保完全保持旧版插件行为。
        content_cleanup_rule = (
            settings.get("content_cleanup_rule", "") if enable_content_cleanup else ""
        )
        content_cleanup_pattern: re.Pattern[str] | None = None
        if content_cleanup_rule:
            try:
                content_cleanup_pattern = re.compile(content_cleanup_rule)
            except re.error:
                logger.error(
                    "[主动消息] 内容清理正则表达式错误，将跳过内容清理并保留原始分段: "
                    f"{traceback.format_exc()}"
                )

        if split_mode == "words":
            # words 模式下，先用分段词列表识别切分点。
            # 注意：这里的“切分”与“内容清理”是两件不同的事：
            # - split_words 负责决定在哪里断句；
            # - content_cleanup_rule 负责决定是否移除分段后的特定字符（如换行）。
            split_words = settings.get("split_words", ["。", "？", "！", "~", "…"])
            if not split_words:
                # 用户未提供分段词时退化为不分段，避免构造空正则导致行为不可预期。
                return [text]

            escaped_words = sorted(
                [re.escape(word) for word in split_words], key=len, reverse=True
            )
            # 保留分隔符，避免语气符号在切分时丢失
            pattern = re.compile(f"(.*?({'|'.join(escaped_words)})|.+$)", re.DOTALL)

            segments = pattern.findall(text)
            result: list[str] = []
            for seg in segments:
                if isinstance(seg, tuple):
                    content = seg[0]
                    if not isinstance(content, str):
                        continue
                    if content_cleanup_pattern:
                        # 这里的 sub 属于“分段后清理”：
                        # content 已经是单个分段，不会再影响其他分段边界。
                        content = content_cleanup_pattern.sub("", content)
                    if content.strip():
                        # 清理后若只剩空白，则直接丢弃，避免发送空消息段。
                        result.append(content)
                elif seg:
                    cleaned_seg = seg
                    if content_cleanup_pattern:
                        cleaned_seg = content_cleanup_pattern.sub("", cleaned_seg)
                    if cleaned_seg.strip():
                        result.append(cleaned_seg)
            return result if result else [text]

        # 正则分段模式
        # regex 仅用于“如何找出每一个分段”，不再假设其天然具备“删除命中字符”的副作用。
        # 若需要删除换行、句号等字符，应通过 content_cleanup_rule 明确声明。
        regex_pattern = settings.get("regex", r".*?[。？！~…\n]+|.+$")
        try:
            split_response = re.findall(regex_pattern, text, re.DOTALL | re.MULTILINE)
        except re.error:
            logger.error(
                f"[主动消息] 分段回复正则表达式错误，使用默认分段方式: {traceback.format_exc()}"
            )
            split_response = re.findall(
                r".*?[。？！~…\n]+|.+$", text, re.DOTALL | re.MULTILINE
            )

        result: list[str] = []
        for seg in split_response:
            cleaned_seg = seg
            if content_cleanup_pattern:
                # 与 words 模式保持一致：先完成切分，再对每段内容做独立清理。
                cleaned_seg = content_cleanup_pattern.sub("", cleaned_seg)
            if cleaned_seg.strip():
                # 过滤掉清理后为空的分段，避免平台收到空 Plain 消息。
                result.append(cleaned_seg)
        return result if result else [text]

    async def _calc_interval(self, text: str, settings: dict) -> float:
        """计算分段回复的间隔时间。"""
        interval_method = settings.get("interval_method", "random")

        # 对数间隔模式（模拟打字速度）
        if interval_method == "log":
            log_base = float(settings.get("log_base", 1.8))
            if all(ord(c) < 128 for c in text):
                word_count = len(text.split())
            else:
                word_count = len([c for c in text if c.isalnum()])
            i = math.log(word_count + 1, log_base)
            return random.uniform(i, i + 0.5)

        # 随机区间模式
        interval_str = settings.get("interval", "1.5, 3.5")
        try:
            interval_ls = [float(t) for t in interval_str.replace(" ", "").split(",")]
            interval = interval_ls if len(interval_ls) == 2 else [1.5, 3.5]
        except Exception:
            interval = [1.5, 3.5]

        return random.uniform(interval[0], interval[1])

    def _segment_decorated_chain(self, chain: list, seg_conf: dict) -> list:
        """对装饰后的完整消息链执行分段。

        只有 Plain 组件参与切分，非文本组件（图片、语音等）原样保留并保持相对顺序。

        Args:
            chain: 已完成装饰的消息链组件列表。
            seg_conf: 分段回复配置。

        Returns:
            切分后的组件列表；若无需切分则返回原列表。
        """
        threshold = seg_conf.get("words_count_threshold", 150)

        # 注意：threshold 的语义是“**不分段字数阈值**”，与字段历史含义保持一致。
        # 文本较长（> threshold）时整段发送，避免长文被切碎影响阅读体验。
        new_chain: list = []
        for comp in chain:
            if not isinstance(comp, Plain):
                new_chain.append(comp)
                continue

            text = comp.text or ""
            if not text.strip():
                continue

            if len(text) > threshold:
                new_chain.append(comp)
                continue

            segments = self._split_text(text, seg_conf)
            if not segments:
                new_chain.append(comp)
                continue

            for seg in segments:
                new_chain.append(Plain(text=seg))

        return new_chain or chain

    # ------------------------------------------------------------------
    # 事件构造
    # ------------------------------------------------------------------
    def _build_proactive_event(self, session_id: str) -> Any:
        """为指定会话构建贯穿全流程的伪事件。"""
        return build_proactive_event_for_session(plugin=self, session_id=session_id)

    async def _run_decorating_hooks(self, event: Any, components: list) -> list:
        """对完整消息链派发 on_decorating_result 钩子。

        先注入初始链，再在钩子执行后回读，允许装饰器整体替换消息链。

        Args:
            event: 贯穿链路的事件对象。
            components: 初始组件列表。

        Returns:
            装饰后的组件列表。
        """
        if event is None or EventType is None:
            return components

        result = MessageEventResult()
        # 关键：标记为 LLM 结果，使依赖 `is_llm_result()` 的装饰器正常工作。
        result.set_result_content_type(ResultContentType.LLM_RESULT)
        result.chain = list(components)
        event.set_result(result)

        try:
            await dispatch_event_hook(event, EventType.OnDecoratingResultEvent)
        except Exception as e:
            logger.error(f"[主动消息] 派发装饰钩子失败喵: {e}")

        decorated = event.get_result()
        if decorated is None:
            logger.debug("[主动消息] 装饰钩子清空了消息结果喵。")
            return []
        chain = getattr(decorated, "chain", None)
        if chain is None:
            return []

        # 回读结果链：装饰器可能整体替换了 chain（含图片等非文本组件）。
        return list(chain)

    # ------------------------------------------------------------------
    # 平台流水补写
    # ------------------------------------------------------------------
    async def _persist_proactive_message_to_platform_history(
        self,
        session_id: str,
        chain: MessageChain,
    ) -> None:
        """将主动消息补写入平台消息流水，弥补部分适配器不会自动持久化的问题。"""
        try:
            parsed = self._parse_session_id(session_id)
        except Exception as e:
            logger.warning(
                f"[主动消息] 解析会话标识失败，跳过平台流水补写喵: {e}",
                exc_info=True,
            )
            return

        if not parsed:
            return

        platform_id, _message_type, target_id = parsed
        history_mgr = getattr(self.context, "message_history_manager", None)
        if not history_mgr or message_chain_to_storage_message_parts is None:
            return

        try:
            db = getattr(history_mgr, "db", None)
            insert_attachment = getattr(db, "insert_attachment", None)
            if not callable(insert_attachment):
                return

            attachments_dir = Path(self.data_dir) / "attachments"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            message_parts = await message_chain_to_storage_message_parts(
                chain,
                insert_attachment=insert_attachment,
                attachments_dir=attachments_dir,
            )
            if not message_parts:
                return

            await history_mgr.insert(
                platform_id=platform_id,
                user_id=target_id,
                content={"type": "bot", "message": message_parts},
                sender_id="bot",
                sender_name="bot",
            )
            logger.debug(
                f"[主动消息] 已将主动消息补写入平台 ({platform_id}) 的流水喵，会话标识为 {target_id}。"
            )
        except Exception as e:
            logger.warning(f"[主动消息] 补写平台流水失败喵: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def _send_chain_direct(self, session_id: str, components: list) -> None:
        """直接通过平台实例发送消息链（不经过事件）。

        仅作为事件不可用时的回退路径，正常流程请使用事件发送，
        以确保第三方插件在装饰/发送后阶段拿到一致的上下文。
        """
        if not components:
            return

        chain = MessageChain(list(components))
        parsed = self._parse_session_id(session_id)
        if not parsed:
            # 无法解析则使用核心 API 兜底
            await self.context.send_message(session_id, chain)
            await self._persist_proactive_message_to_platform_history(session_id, chain)
            return

        p_id, m_type_str, t_id = parsed
        if MS is None:  # pragma: no cover - 极旧版本
            await self.context.send_message(session_id, chain)
            await self._persist_proactive_message_to_platform_history(session_id, chain)
            return

        from astrbot.core.platform.message_type import MessageType

        m_type = (
            MessageType.GROUP_MESSAGE
            if is_group_session(session_id)
            else MessageType.FRIEND_MESSAGE
        )

        # 精确匹配平台实例：避免将消息发往错误平台
        platforms = self.context.platform_manager.get_insts()
        target_platform = next((p for p in platforms if p.meta().id == p_id), None)

        if not target_platform:
            logger.warning(
                f"[主动消息] 找不到指定的平台 {p_id} 喵，尝试使用核心 API 兜底喵。"
            )
            await self.context.send_message(session_id, chain)
            await self._persist_proactive_message_to_platform_history(session_id, chain)
            return

        if target_platform.status != PlatformStatus.RUNNING:
            logger.warning(f"[主动消息] 平台 {p_id} 未运行喵，跳过主动消息喵。")
            return

        try:
            session_obj = MS(platform_name=p_id, message_type=m_type, session_id=t_id)
            await target_platform.send_by_session(session_obj, chain)
            logger.debug(f"[主动消息] 消息将通过平台 {p_id} 送达喵")
            if p_id != "webchat":
                await self._persist_proactive_message_to_platform_history(
                    session_id, chain
                )
        except Exception as e:
            logger.error(f"[主动消息] 通过平台 {p_id} 发送失败喵: {e}")
            logger.debug(traceback.format_exc())
            if self.telemetry and self.telemetry.enabled:
                # 平台发送失败是实际送达链路的问题，与 LLM 生成失败应在遥测上分开统计。
                self._track_task(
                    asyncio.create_task(
                        self.telemetry.track_error(
                            e,
                            module="core.message_sender._send_chain_direct",
                        )
                    )
                )

    async def _send_chain(
        self,
        session_id: str,
        event: Any,
        components: list,
    ) -> None:
        """发送一条消息链（优先走事件，事件不可用时回退平台直发）。"""
        if not components:
            return

        chain = MessageChain(list(components))

        if event is not None:
            try:
                await event.send(chain)
                parsed = self._parse_session_id(session_id)
                if parsed and parsed[0] != "webchat":
                    await self._persist_proactive_message_to_platform_history(
                        session_id, chain
                    )
                return
            except Exception as e:
                logger.error(f"[主动消息] 事件发送失败喵，回退平台直发: {e}")

        await self._send_chain_direct(session_id, components)

    async def _send_proactive_message(
        self,
        session_id: str,
        text: str,
        event: Any = None,
        initial_chain: list | None = None,
    ) -> None:
        """发送主动消息（支持 TTS 与分段）。

        流程严格对齐官方语义：

        1. TTS（可选）：语音作为独立形态直接投递；
        2. 文本：先对完整消息链派发装饰钩子，再按配置分段；
        3. 全部发送完成后派发 after_message_sent，供装饰器补发后续内容。

        Args:
            session_id: 会话 UMO。
            text: 生成的主动消息文本。
            event: 贯穿本轮的伪事件；为 None 时会尝试内部构建。
            initial_chain: Provider 直接返回的消息链组件。
                仅当文本为空时作为初始链使用（例如纯图片输出场景）。
        """
        session_config = self._get_session_config(session_id)
        if not session_config:
            logger.info(
                f"[主动消息] 无法获取会话配置，跳过 {self._get_session_log_str(session_id)} 的消息发送喵。"
            )
            return

        if event is None:
            event = self._build_proactive_event(session_id)

        logger.info(
            f"[主动消息] 开始发送 {self._get_session_log_str(session_id, session_config)} 的主动消息喵。"
        )

        tts_conf = session_config.get("tts_settings", {})
        seg_conf = session_config.get("segmented_reply_settings", {})
        text = text or ""

        # 先尝试 TTS：成功后是否继续发文本由 always_send_text 控制。
        # TTS 属于发送形态转换，不参与文本装饰，避免装饰器把语音再转成文本。
        is_tts_sent = False
        if tts_conf.get("enable_tts", True) and text.strip():
            try:
                logger.info("[主动消息] 尝试进行手动TTS喵。")
                tts_provider = self.context.get_using_tts_provider(umo=session_id)
                if tts_provider:
                    audio_path = await tts_provider.get_audio(text)
                    if audio_path:
                        await self._send_chain(
                            session_id, event, [Record(file=audio_path)]
                        )
                        is_tts_sent = True
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[主动消息] 手动TTS流程发生异常喵: {e}")
                if self.telemetry and self.telemetry.enabled:
                    # TTS 失败不一定意味着文本发送失败，因此单独挂到 tts 子模块下记录。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_error(
                                e,
                                module="core.message_sender._send_proactive_message.tts",
                            )
                        )
                    )

        # 是否继续发送文本：未发出 TTS 或配置要求始终发文本
        should_send_text = not is_tts_sent or tts_conf.get("always_send_text", True)

        if should_send_text:
            # 构造初始消息链：
            # 有文本时，以文本为起点，让装饰器看到完整内容；
            # 无文本但 Provider 返回了组件时，直接以该组件链为起点；
            # 两者皆无时，初始链为空，装饰器仍可自行补充内容。
            if text.strip():
                base_components: list = [Plain(text=text)]
            elif initial_chain:
                base_components = list(initial_chain)
            else:
                base_components = []

            # 步骤一：对完整消息链执行装饰，让装饰器看到完整文本。
            decorated_chain = await self._run_decorating_hooks(event, base_components)
            if not decorated_chain:
                logger.debug("[主动消息] 装饰后消息链为空，跳过文本发送喵。")

            # 步骤二：按配置对装饰后的链执行分段。
            enable_seg = seg_conf.get("enable", False)
            send_chain = decorated_chain
            segmented = False
            if enable_seg and decorated_chain:
                send_chain = self._segment_decorated_chain(decorated_chain, seg_conf)
                # 仅当分段导致组件数量变化时，才认为确实执行了分段。
                segmented = len(send_chain) != len(decorated_chain)

            if decorated_chain:
                if segmented:
                    logger.info(
                        f"[主动消息] 分段回复已启用，将发送 {len(send_chain)} 条消息喵。"
                    )
                if self.telemetry and self.telemetry.enabled:
                    # 这里只记录分段数、文本长度、TTS 开关等统计值，不上传任何消息正文内容。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_feature(
                                "message_send_result",
                                {
                                    "session_type": session_config.get(
                                        "_session_type", "unknown"
                                    ),
                                    "tts_enabled": bool(
                                        tts_conf.get("enable_tts", True)
                                    ),
                                    "tts_sent": is_tts_sent,
                                    "segmented_enabled": segmented,
                                    "segment_count": len(send_chain),
                                    "text_length": len(text),
                                    "success": True,
                                },
                            )
                        )
                    )

                if segmented:
                    # 分段顺序发送，段间按策略等待，模拟自然输出节奏。
                    for idx, comp in enumerate(send_chain):
                        await self._send_chain(session_id, event, [comp])
                        if idx < len(send_chain) - 1:
                            interval = await self._calc_interval(
                                getattr(comp, "text", "") or "", seg_conf
                            )
                            logger.debug(
                                f"[主动消息] 分段回复等待 {interval:.2f} 秒喵。"
                            )
                            await asyncio.sleep(interval)
                else:
                    await self._send_chain(session_id, event, send_chain)

        # 发送后钩子：装饰器可据此补发延迟内容（如分段表情图片）。
        if event is not None and EventType is not None:
            try:
                await dispatch_event_hook(event, EventType.OnAfterMessageSentEvent)
            except Exception as e:
                logger.error(f"[主动消息] 派发发送后钩子失败喵: {e}")

        # 清理结果残留，与官方 respond 阶段的收尾语义保持一致，
        # 避免事件被复用（或异常重试）时携带上一轮的消息链。
        if event is not None:
            try:
                event.clear_result()
            except Exception:
                pass

        # Bot 在群聊发言后需要重置沉默计时
        if is_group_session(session_id):
            await self._reset_group_silence_timer(session_id)
            logger.info(
                f"[主动消息] Bot主动消息已发送，已重置 {self._get_session_log_str(session_id, session_config)} 的沉默倒计时喵。"
            )
