"""发送与装饰钩子服务。

负责发送入口、装饰钩子派发与平台流水补写。消息链的清洗、分段与
批次规划已下沉到 DeliveryPlanner，逐批发送与 TTS 顺序由 DeliveryExecutor
执行，本服务只做编排与落库。
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
from ..adapters.astrbot_ltm import read_group_history_settings, supports_max_messages
from ..adapters.astrbot_platform import find_platform_by_id
from ..domain.delivery_plan import format_media_summary
from ..domain.delivery_report import DeliveryReport
from ..domain.session_key import is_group_umo
from ..domain.tool_markup_cleaner import clean_chain_components, clean_llm_text
from .delivery_executor import build_component_chain

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
        enable_content_cleanup = settings.get("content_cleanup_enabled", False)
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
        via_core_api: bool = False,
    ) -> None:
        """将主动消息补写入平台消息流水。

        主动消息不经过真实事件，平台侧不会自动落库，故在此显式补写；
        webchat 平台与缺少历史管理器时静默跳过。

        via_core_api 为 True 表示消息已由核心 context.send_message 送出，
        该接口在群聊场景下会自行落库，此时跳过补写以避免同键双写。
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
        # 与 AstrBot 核心保持一致：核心以完整 UMO（str(session)）作为平台流水的 user_id。
        storage_user_id = (
            session_id
            if isinstance(session_id, str) and session_id.strip()
            else target_id
        )
        if platform_id == "webchat":
            return
        history_mgr = getattr(plugin.context, "message_history_manager", None)
        if not history_mgr or message_chain_to_storage_message_parts is None:
            return

        # 群聊判定基于完整 UMO 的类型段，避免漏判后绕过下面的开关与去重检查。
        is_group_session = is_group_umo(storage_user_id)

        # 群聊补写必须跟随核心开关：关闭时核心从不写入真实群友消息，
        # 单独补写只会留下核心查询侧不认的孤儿数据。
        # 注意：核心 send_message 仅在群聊场景落库，私聊仍需插件补写。
        max_messages: int | None = None
        if is_group_session:
            enabled, max_messages = read_group_history_settings(
                plugin.context, storage_user_id
            )
            if not enabled:
                logger.debug(
                    "[主动消息] 核心未启用群聊消息持久化，跳过平台流水补写喵。"
                )
                return
            # 核心已通过 send_message 落库，重复补写会让同一条消息存两份。
            if via_core_api:
                logger.debug("[主动消息] 消息已由核心 API 落库，跳过重复补写喵。")
                return

        try:
            db = getattr(history_mgr, "db", None)
            insert_attachment = getattr(db, "insert_attachment", None)
            if not callable(insert_attachment):
                return

            attachments_dir = Path(plugin.data_dir) / "attachments"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            message_parts = await self._build_storage_parts(
                session_id, chain, insert_attachment, attachments_dir
            )
            if not message_parts:
                return

            insert_kwargs: dict[str, Any] = {
                "platform_id": platform_id,
                "user_id": storage_user_id,
                "content": {"type": "bot", "message": message_parts},
                "sender_id": "bot",
                "sender_name": "bot",
            }
            # 旧版核心 insert() 没有 max_messages 参数，传入会抛 TypeError。
            if max_messages is not None and supports_max_messages(history_mgr):
                insert_kwargs["max_messages"] = max_messages

            await history_mgr.insert(**insert_kwargs)
            logger.debug(
                f"[主动消息] 已将主动消息补写入平台 ({platform_id}) 的流水喵，会话标识为 {storage_user_id}。"
            )
        except Exception as e:
            logger.warning(f"[主动消息] 补写平台流水失败喵: {e}", exc_info=True)

    async def _build_storage_parts(
        self,
        session_id: str,
        chain: MessageChain,
        insert_attachment: Any,
        attachments_dir: Path,
    ) -> list:
        """逐组件转换落库分段，单个媒体失败只跳过该组件。

        message_chain_to_storage_message_parts 内部会下载 URL、转码音频，
        任一组件抛错都会让整条流水写入失败，因此这里按单组件隔离并加超时。
        """
        if message_chain_to_storage_message_parts is None:
            return []

        timeout = self._media_timeout_seconds(session_id)
        parts: list = []
        for comp in list(getattr(chain, "chain", None) or []):
            single = MessageChain([comp])
            try:
                produced = await asyncio.wait_for(
                    message_chain_to_storage_message_parts(
                        single,
                        insert_attachment=insert_attachment,
                        attachments_dir=attachments_dir,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[主动消息] 媒体落库超过 {timeout:.0f} 秒已跳过该组件喵。"
                )
                continue
            except Exception as e:  # noqa: BLE001 - 单个媒体失败不应影响其它组件
                logger.warning(f"[主动消息] 媒体落库失败，已跳过该组件喵: {e}")
                continue
            if produced:
                parts.extend(produced)
        return parts

    def _media_timeout_seconds(self, session_id: str) -> float:
        """读取媒体落库超时配置，缺省 30 秒。"""
        try:
            session_config = (
                self.plugin.config_service.get_session_config(session_id) or {}
            )
        except Exception:  # pragma: no cover - 配置服务异常时用默认值
            session_config = {}
        try:
            planner = self._planner()
            return float(planner.get_settings(session_config)["media_timeout_seconds"])
        except Exception:  # pragma: no cover
            return 30.0

    def _planner(self) -> Any:
        """惰性获取投递计划服务。"""
        planner = getattr(self.plugin, "delivery_planner", None)
        if planner is not None:
            return planner
        from .delivery_planner import DeliveryPlanner

        return DeliveryPlanner(self.plugin)

    def _executor(self) -> Any:
        """惰性获取投递执行服务。"""
        executor = getattr(self.plugin, "delivery_executor", None)
        if executor is not None:
            return executor
        from .delivery_executor import DeliveryExecutor

        return DeliveryExecutor(self.plugin)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def send_chain_direct(
        self,
        session_id: str,
        components: list,
        meta: dict | None = None,
    ) -> bool:
        """直接通过平台实例发送消息链（不经过事件）。

        优先定位 UMO 对应的平台实例直发；平台缺失、未运行或发送异常时
        回退到核心发送 API，确保尽力送达。
        """
        plugin = self.plugin
        if not components:
            return False

        chain = build_component_chain(components, meta or {})
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
            # 此处走平台实例直发，不经过核心 send_message，核心不会落库，
            # 必须由插件补写，否则主动消息不会出现在平台流水中。
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

        # 核心 send_message 在群聊场景下会自行落库，标记来源避免同键双写。
        await self.persist_proactive_message_to_platform_history(
            session_id, chain, via_core_api=True
        )
        return True

    async def send_chain(
        self,
        session_id: str,
        event: Any,
        components: list,
        meta: dict | None = None,
    ) -> bool:
        """发送一条消息链（优先走事件，事件不可用时回退平台直发）。

        事件返回 None 视为“已接管发送”，此时需补写平台流水；
        返回 False 视为失败，回退直发并标记事件状态。

        meta 用于携带装饰钩子设置的链级元数据（use_t2i_ 等）。
        """
        if not components:
            return False

        chain = build_component_chain(components, meta or {})

        if event is not None:
            try:
                sent = await event.send(chain)
            except Exception as e:
                logger.error(f"[主动消息] 事件发送异常喵，回退平台直发: {e}")
                sent_direct = await self.send_chain_direct(session_id, components, meta)
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

        return await self.send_chain_direct(session_id, components, meta)

    async def send_proactive_message(
        self,
        session_id: str,
        text: str,
        event: Any = None,
        initial_chain: list | None = None,
        report: DeliveryReport | None = None,
        outcome: Any = None,
    ) -> bool:
        """发送主动消息（富媒体 + TTS + 分段）。

        流程：构建投递计划（装饰钩子 / 清洗 / 剥离 / 分段 / 分组 / TTS 判定）
        -> 执行计划逐批发送 -> 失败降级 -> 发送后钩子。

        report 非空时回填真实统计；outcome 非空时回填媒体摘要，
        供 RECORD 阶段在无文本轮次写入对话历史。
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

        # 发送前最后一道防线：装饰钩子与外部调用方都可能绕过 DELIBERATE 的清洗。
        text = clean_llm_text(text or "")
        if initial_chain:
            initial_chain = clean_chain_components(initial_chain)

        planner = self._planner()
        settings = planner.get_settings(session_config)
        plan = await planner.build_plan(
            session_id=session_id,
            session_config=session_config,
            text=text,
            initial_chain=initial_chain,
            event=event,
            sender_service=self,
        )

        if report is not None:
            report.stopped_by_hook = plan.blocked_reason == "stopped_by_hook"
            report.sent = False
            if plan.warnings:
                report.metadata.setdefault("plan_warnings", list(plan.warnings))

        # 装饰钩子主动清空了结果：旧行为是安静跳过，这里保持等价。
        # 不能当成发送失败，否则会把一次成功的生成误判为失败并重调度。
        if plan.blocked_reason == "decorated_chain_empty":
            logger.info("[主动消息] 装饰后消息链为空，本次不发送任何内容喵。")
            self._clear_event_result(event)
            return True

        if plan.blocked_reason == "stopped_by_hook":
            logger.info("[主动消息] 装饰钩子终止了事件传播，已放弃本次发送喵。")

        # 组件被策略全部剥离：没有可投递内容，按未送达处理但不重发。
        if plan.blocked_reason == "no_sendable_component":
            logger.warning(
                "[主动消息] 消息链中的组件全部被策略剥离，本次无可投递内容喵。"
            )
            if report is not None:
                report.dropped_components = list(plan.dropped)
            self._clear_event_result(event)
            return False

        any_sent = await self._executor().execute_plan(
            session_id=session_id,
            plan=plan,
            event=event,
            report=report,
            sender_service=self,
            settings=settings,
        )

        # 只有真正送达过内容才记录媒体摘要：未送达的轮次不应污染对话历史。
        # 计数以报告里的实际送达为准：媒体可能被降级或整批失败。
        sent_media = report.media_sent if report is not None else {}
        if outcome is not None and any_sent and sent_media:
            outcome.media_counts = dict(sent_media)
            outcome.media_summary = format_media_summary(sent_media)

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
                                (session_config.get("tts_settings") or {}).get(
                                    "enabled", False
                                )
                            ),
                            "tts_sent": bool(report.voice_sent) if report else False,
                            "segmented_enabled": bool(plan.segmented),
                            "segment_count": len(plan.batches),
                            "text_length": len(text),
                            "media_total": plan.media_total,
                            "media_components": ",".join(sorted(plan.media_counts)),
                            "media_degraded": (report.media_degraded if report else 0),
                            "dropped_components": len(plan.dropped),
                            "success": any_sent,
                        },
                    )
                )
            )

        # 仅在确实送出过消息、且事件未被钩子终止时派发发送后钩子：
        # dispatch_event_hook 会先执行 handler 再检查停止状态，已停止的事件
        # 仍可能触发首个 handler 的副作用，故这里前置判断。
        if (
            any_sent
            and event is not None
            and EventType is not None
            and not self._safe_is_stopped(event)
        ):
            try:
                await dispatch_event_hook(event, EventType.OnAfterMessageSentEvent)
            except Exception as e:
                logger.error(f"[主动消息] 派发发送后钩子失败喵: {e}")

        self._clear_event_result(event)

        if report is not None:
            report.sent = bool(any_sent)

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
