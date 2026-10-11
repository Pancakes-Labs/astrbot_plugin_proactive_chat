"""发送与装饰钩子服务。

由旧 core/message_sender.py 等价搬迁而来；分段、装饰、TTS 与
平台流水补写逻辑保持不变。
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

from ..adapters.astrbot_event import (
    build_proactive_event_for_session,
    dispatch_event_hook,
    resolve_message_type,
)
from ..adapters.astrbot_platform import find_platform_by_id
from ..domain.session_key import is_group_umo

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


class SenderService:
    """主动消息发送与装饰钩子服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 文本分段
    # ------------------------------------------------------------------
    def split_text(self, text: str, settings: dict) -> list[str]:
        """根据配置对文本进行分段。

        支持两种模式：按词（words）切分与按正则（regex）切分。
        正则编译失败时记录日志并回退默认规则，保证仍能返回可用分段。
        """
        split_mode = settings.get("split_mode", "regex")

        # 可选内容清理：先编译规则，非法正则按“不清理”处理。
        enable_content_cleanup = settings.get("enable_content_cleanup", False)
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

        # 按词切分：把分隔词转义后拼成正则，长词优先以匹配最长分隔符。
        if split_mode == "words":
            split_words = settings.get("split_words", ["。", "？", "！", "~", "…"])
            if not split_words:
                return [text]

            escaped_words = sorted(
                [re.escape(word) for word in split_words], key=len, reverse=True
            )
            pattern = re.compile(f"(.*?({'|'.join(escaped_words)})|.+$)", re.DOTALL)

            segments = pattern.findall(text)
            result: list[str] = []
            for seg in segments:
                if isinstance(seg, tuple):
                    content = seg[0]
                    if not isinstance(content, str):
                        continue
                    if content_cleanup_pattern:
                        content = content_cleanup_pattern.sub("", content)
                    if content.strip():
                        result.append(content)
                elif seg:
                    cleaned_seg = seg
                    if content_cleanup_pattern:
                        cleaned_seg = content_cleanup_pattern.sub("", cleaned_seg)
                    if cleaned_seg.strip():
                        result.append(cleaned_seg)
            return result if result else [text]

        # 按正则切分（默认按句末标点与换行）。
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
                cleaned_seg = content_cleanup_pattern.sub("", cleaned_seg)
            if cleaned_seg.strip():
                result.append(cleaned_seg)
        return result if result else [text]

    async def calc_interval(self, text: str, settings: dict) -> float:
        """计算分段回复的间隔时间。

        log 模式按文本长度对数缩放，random 模式在配置区间内均匀随机。
        """
        interval_method = settings.get("interval_method", "random")

        if interval_method == "log":
            log_base = float(settings.get("log_base", 1.8))
            if all(ord(c) < 128 for c in text):
                word_count = len(text.split())
            else:
                word_count = len([c for c in text if c.isalnum()])
            i = math.log(word_count + 1, log_base)
            return random.uniform(i, i + 0.5)

        interval_str = settings.get("interval", "1.5, 3.5")
        try:
            interval_ls = [float(t) for t in interval_str.replace(" ", "").split(",")]
            interval = interval_ls if len(interval_ls) == 2 else [1.5, 3.5]
        except Exception:
            interval = [1.5, 3.5]

        return random.uniform(interval[0], interval[1])

    def segment_decorated_chain(self, chain: list, seg_conf: dict) -> list:
        """对装饰后的完整消息链执行分段。

        仅对短于阈值的纯文本段做切分；超长或非文本成分原样保留。
        """
        threshold = seg_conf.get("words_count_threshold", 150)

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

            segments = self.split_text(text, seg_conf)
            if not segments:
                new_chain.append(comp)
                continue

            for seg in segments:
                new_chain.append(Plain(text=seg))

        return new_chain or chain

    # ------------------------------------------------------------------
    # 事件构造
    # ------------------------------------------------------------------
    def build_proactive_event(self, session_id: str) -> Any:
        """为指定会话构建贯穿全流程的伪事件。"""
        return build_proactive_event_for_session(
            plugin=self.plugin, session_id=session_id
        )

    @staticmethod
    def _mark_event_send_failed(event: Any) -> None:
        if event is None:
            return
        try:
            if hasattr(event, "proactive_send_failed"):
                event.proactive_send_failed = True
        except Exception:  # pragma: no cover - 未知事件实现
            pass

    @staticmethod
    def _mark_event_sent(event: Any) -> None:
        if event is None:
            return
        try:
            event._has_send_oper = True  # noqa: SLF001 - 与官方事件语义对齐
        except Exception:  # pragma: no cover - 未知事件实现
            pass

    @staticmethod
    def _safe_is_stopped(event: Any) -> bool:
        try:
            return bool(event.is_stopped())
        except Exception:
            return False

    @staticmethod
    def _clear_event_result(event: Any) -> None:
        if event is None:
            return
        try:
            event.clear_result()
        except Exception:
            pass

    async def run_decorating_hooks(
        self, event: Any, components: list
    ) -> tuple[list, bool]:
        """对完整消息链派发 on_decorating_result 钩子。"""
        if event is None or EventType is None:
            return components, True

        result = MessageEventResult()
        result.set_result_content_type(ResultContentType.LLM_RESULT)
        result.chain = list(components)
        event.set_result(result)

        stopped = False
        try:
            stopped = await dispatch_event_hook(
                event, EventType.OnDecoratingResultEvent
            )
        except Exception as e:
            logger.error(f"[主动消息] 派发装饰钩子失败喵: {e}")

        if stopped or self._safe_is_stopped(event):
            logger.info(
                "[主动消息] 装饰钩子终止了事件传播，已放弃本次主动消息的发送喵。"
            )
            return [], False

        decorated = event.get_result()
        if decorated is None:
            logger.debug("[主动消息] 装饰钩子清空了消息结果喵。")
            return [], True
        chain = getattr(decorated, "chain", None)
        if chain is None:
            return [], True

        return list(chain), True

    # ------------------------------------------------------------------
    # 平台流水补写
    # ------------------------------------------------------------------
    async def persist_proactive_message_to_platform_history(
        self,
        session_id: str,
        chain: MessageChain,
    ) -> None:
        """将主动消息补写入平台消息流水。

        主动消息不经过真实事件，平台侧不会自动落库，故在此显式补写；
        webchat 平台与缺少历史管理器时静默跳过。
        """
        plugin = self.plugin
        try:
            parsed = plugin.session_service.parse_session_id(session_id)
        except Exception as e:
            logger.warning(
                f"[主动消息] 解析会话标识失败，跳过平台流水补写喵: {e}",
                exc_info=True,
            )
            return

        if not parsed:
            return

        platform_id, _message_type, target_id = parsed
        if platform_id == "webchat":
            return
        history_mgr = getattr(plugin.context, "message_history_manager", None)
        if not history_mgr or message_chain_to_storage_message_parts is None:
            return

        try:
            db = getattr(history_mgr, "db", None)
            insert_attachment = getattr(db, "insert_attachment", None)
            if not callable(insert_attachment):
                return

            attachments_dir = Path(plugin.data_dir) / "attachments"
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
    async def send_chain_direct(self, session_id: str, components: list) -> bool:
        """直接通过平台实例发送消息链（不经过事件）。

        优先定位 UMO 对应的平台实例直发；平台缺失、未运行或发送异常时
        回退到核心发送 API，确保尽力送达。
        """
        plugin = self.plugin
        if not components:
            return False

        chain = MessageChain(list(components))
        parsed = plugin.session_service.parse_session_id(session_id)
        if not parsed:
            return await self.send_chain_via_core_api(session_id, chain)

        p_id, m_type_str, t_id = parsed
        if MS is None:  # pragma: no cover - 极旧版本
            return await self.send_chain_via_core_api(session_id, chain)

        m_type = resolve_message_type(m_type_str)

        target_platform = find_platform_by_id(plugin.context.platform_manager, p_id)

        if not target_platform:
            logger.warning(
                f"[主动消息] 找不到指定的平台 {p_id} 喵，尝试使用核心 API 兜底喵。"
            )
            return await self.send_chain_via_core_api(session_id, chain)

        if target_platform.status != PlatformStatus.RUNNING:
            logger.warning(f"[主动消息] 平台 {p_id} 未运行喵，跳过主动消息喵。")
            return False

        try:
            session_obj = MS(platform_name=p_id, message_type=m_type, session_id=t_id)
            await target_platform.send_by_session(session_obj, chain)
            logger.debug(f"[主动消息] 消息将通过平台 {p_id} 送达喵")
            await self.persist_proactive_message_to_platform_history(session_id, chain)
            return True
        except Exception as e:
            logger.error(f"[主动消息] 通过平台 {p_id} 发送失败喵: {e}")
            logger.debug(traceback.format_exc())
            if plugin.telemetry and plugin.telemetry.enabled:
                plugin._track_task(
                    asyncio.create_task(
                        plugin.telemetry.track_error(
                            e,
                            module="core.message_sender._send_chain_direct",
                        )
                    )
                )
            return await self.send_chain_via_core_api(session_id, chain)

    async def send_chain_via_core_api(
        self, session_id: str, chain: MessageChain
    ) -> bool:
        """通过核心发送 API 兜底发送，并保证“失败”是可感知的。

        与直发一样，成功后补写平台流水。返回 False 表示确实未送达。
        """
        plugin = self.plugin
        try:
            result = await plugin.context.send_message(session_id, chain)
        except Exception as e:
            logger.error(f"[主动消息] 核心 API 发送失败喵: {e}")
            return False

        if result is False:
            logger.error(
                f"[主动消息] 核心 API 未能找到匹配平台，消息未送达喵: {session_id}"
            )
            return False

        await self.persist_proactive_message_to_platform_history(session_id, chain)
        return True

    async def send_chain(
        self,
        session_id: str,
        event: Any,
        components: list,
    ) -> bool:
        """发送一条消息链（优先走事件，事件不可用时回退平台直发）。

        事件返回 None 视为“已接管发送”，此时需补写平台流水；
        返回 False 视为失败，回退直发并标记事件状态。
        """
        if not components:
            return False

        chain = MessageChain(list(components))

        if event is not None:
            try:
                sent = await event.send(chain)
            except Exception as e:
                logger.error(f"[主动消息] 事件发送异常喵，回退平台直发: {e}")
                sent_direct = await self.send_chain_direct(session_id, components)
                if sent_direct:
                    self._mark_event_sent(event)
                else:
                    self._mark_event_send_failed(event)
                return sent_direct

            if sent is True:
                return True
            if sent is None:
                await self.persist_proactive_message_to_platform_history(
                    session_id, chain
                )
                return True

            self._mark_event_send_failed(event)
            logger.error("[主动消息] 事件发送与核心 API 兜底均未送达，不再重复尝试喵。")
            return False

        return await self.send_chain_direct(session_id, components)

    async def send_proactive_message(
        self,
        session_id: str,
        text: str,
        event: Any = None,
        initial_chain: list | None = None,
    ) -> bool:
        """发送主动消息（支持 TTS 与分段）。

        流程：可选 TTS 语音 -> 装饰钩子 -> 可选分段 -> 逐段发送 ->
        发送后钩子。任一环节实际送出至少一条即视为成功。
        """
        plugin = self.plugin
        session_config = plugin.config_service.get_session_config(session_id)
        if not session_config:
            logger.info(
                f"[主动消息] 无法获取会话配置，跳过 {plugin.session_service.get_session_log_str(session_id)} 的消息发送喵。"
            )
            return False

        if event is None:
            event = self.build_proactive_event(session_id)

        logger.info(
            f"[主动消息] 开始发送 {plugin.session_service.get_session_log_str(session_id, session_config)} 的主动消息喵。"
        )

        tts_conf = session_config.get("tts_settings", {})
        seg_conf = session_config.get("segmented_reply_settings", {})
        text = text or ""

        # 可选语音发送：合成成功即作为一条消息链直接送出。
        is_tts_sent = False
        if tts_conf.get("enable_tts", True) and text.strip():
            try:
                logger.info("[主动消息] 尝试进行手动TTS喵。")
                tts_provider = plugin.context.get_using_tts_provider(umo=session_id)
                if tts_provider:
                    audio_path = await tts_provider.get_audio(text)
                    if audio_path:
                        is_tts_sent = (
                            await self.send_chain(
                                session_id, event, [Record(file=audio_path)]
                            )
                            is True
                        )
                        if is_tts_sent:
                            await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[主动消息] 手动TTS流程发生异常喵: {e}")
                if plugin.telemetry and plugin.telemetry.enabled:
                    plugin._track_task(
                        asyncio.create_task(
                            plugin.telemetry.track_error(
                                e,
                                module="core.message_sender._send_proactive_message.tts",
                            )
                        )
                    )

        should_send_text = not is_tts_sent or tts_conf.get("always_send_text", True)

        any_sent = is_tts_sent

        if should_send_text:
            base_components: list = list(initial_chain) if initial_chain else []

            # 初始链未包含文本时，在开头补入纯文本段。
            text_value = text.strip()
            if text_value and not any(
                isinstance(comp, Plain) for comp in base_components
            ):
                base_components.insert(0, Plain(text=text_value))

            decorated_chain, should_send = await self.run_decorating_hooks(
                event, base_components
            )
            if not should_send:
                self._clear_event_result(event)
                return False
            if not decorated_chain:
                logger.debug("[主动消息] 装饰后消息链为空，跳过文本发送喵。")

            # 可选分段：分段后逐条发送，条目数与装饰后链不同即认为生效。
            enable_seg = seg_conf.get("enable", False)
            send_chain = decorated_chain
            segmented = False
            if enable_seg and decorated_chain:
                send_chain = self.segment_decorated_chain(decorated_chain, seg_conf)
                segmented = len(send_chain) != len(decorated_chain)

            if decorated_chain:
                if segmented:
                    logger.info(
                        f"[主动消息] 分段回复已启用，将发送 {len(send_chain)} 条消息喵。"
                    )

                if segmented:
                    for idx, comp in enumerate(send_chain):
                        if await self.send_chain(session_id, event, [comp]) is True:
                            any_sent = True
                        if idx < len(send_chain) - 1:
                            interval = await self.calc_interval(
                                getattr(comp, "text", "") or "", seg_conf
                            )
                            logger.debug(
                                f"[主动消息] 分段回复等待 {interval:.2f} 秒喵。"
                            )
                            await asyncio.sleep(interval)
                elif await self.send_chain(session_id, event, send_chain) is True:
                    any_sent = True

                if plugin.telemetry and plugin.telemetry.enabled:
                    plugin._track_task(
                        asyncio.create_task(
                            plugin.telemetry.track_feature(
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
                                    "success": any_sent,
                                },
                            )
                        )
                    )

        if event is not None and EventType is not None:
            try:
                await dispatch_event_hook(event, EventType.OnAfterMessageSentEvent)
            except Exception as e:
                logger.error(f"[主动消息] 派发发送后钩子失败喵: {e}")

        self._clear_event_result(event)

        if not any_sent:
            logger.error(
                f"[主动消息] {plugin.session_service.get_session_log_str(session_id, session_config)} 的主动消息未能送达任何平台喵。"
            )
            return False

        # 群聊发送成功后重置沉默倒计时，等待下一轮静默再触发。
        if is_group_umo(session_id):
            await plugin.scheduler_service.reset_group_silence_timer(session_id)
            logger.info(
                f"[主动消息] Bot主动消息已发送，已重置 {plugin.session_service.get_session_log_str(session_id, session_config)} 的沉默倒计时喵。"
            )
        return True
