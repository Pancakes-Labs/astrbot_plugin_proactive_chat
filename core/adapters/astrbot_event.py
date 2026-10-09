"""AstrBot 事件适配器：伪事件构造与标准钩子派发。

本模块由旧 core/proactive_event.py 等价搬迁而来，行为保持一致。
"""

from __future__ import annotations

import inspect
import traceback
import uuid
from typing import Any

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform import PlatformStatus

from ..domain.session_key import SessionKey, is_group_umo
from .astrbot_platform import resolve_platform_instance

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
except ImportError:  # pragma: no cover
    try:
        from astrbot.api.event import AstrMessageEvent
    except ImportError:
        AstrMessageEvent = None  # type: ignore[assignment]

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.platform.astr_message_event import MessageSession
except ImportError:  # pragma: no cover
    try:
        from astrbot.core.platform.message_session import MessageSession
    except ImportError:
        MessageSession = None  # type: ignore[assignment]

try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.star.star_handler import EventType, star_handlers_registry
except ImportError:  # pragma: no cover
    EventType = None  # type: ignore[assignment]
    star_handlers_registry = None  # type: ignore[assignment]


# 当基类不可用时（极旧版本），退化到普通对象，保证插件不会因为构造事件而崩溃。
_EventBase: Any = AstrMessageEvent if AstrMessageEvent is not None else object

# 伪消息 ID 前缀：便于在日志与平台流水中区分主动消息与真实用户消息。
PROACTIVE_MESSAGE_ID_PREFIX = "proactive"


def resolve_message_type(umo: str) -> MessageType:
    """根据 UMO 推断标准 MessageType（群聊或私聊）。"""
    return (
        MessageType.GROUP_MESSAGE if is_group_umo(umo) else MessageType.FRIEND_MESSAGE
    )


def resolve_self_id(plugin: Any, umo: str) -> str:
    """解析机器人自身 ID。

    先按精确 UMO 查找；未命中时按“同目标”兜底，兼容键规范化漂移。
    全部失败时返回占位值 bot。
    """
    session_data = getattr(plugin, "session_data", None)
    if isinstance(session_data, dict):
        payload = session_data.get(umo) or {}
        self_id = str(payload.get("self_id") or "").strip()
        if self_id:
            return self_id
        # 兼容规范化键漂移：按“同目标”兜底查找
        target = umo.rsplit(":", 1)[-1] if ":" in umo else ""
        if target:
            for key, value in session_data.items():
                if not str(key).endswith(f":{target}"):
                    continue
                if not isinstance(value, dict):
                    continue
                candidate = str(value.get("self_id") or "").strip()
                if candidate:
                    return candidate
    return "bot"


def resolve_sender_hint(plugin: Any, umo: str) -> tuple[str, str]:
    """解析主动消息的发送者占位信息（最近一位真实发送者）。"""
    session_data = getattr(plugin, "session_data", None)
    payload = session_data.get(umo) if isinstance(session_data, dict) else None
    if isinstance(payload, dict):
        sender_id = str(payload.get("last_sender_id") or "").strip()
        sender_name = str(payload.get("last_sender_name") or "").strip()
        if sender_id or sender_name:
            return sender_id, sender_name
    return "", ""


async def dispatch_event_hook(
    event: Any,
    hook_type: Any,
    *args: Any,
    **kwargs: Any,
) -> bool:
    """派发 AstrBot 标准事件钩子。

    行为与官方 call_event_hook 保持一致；额外做异常隔离与旧版本兼容。
    """
    if event is None or hook_type is None or star_handlers_registry is None:
        return False

    try:
        handlers = star_handlers_registry.get_handlers_by_event_type(
            hook_type,
            plugins_name=getattr(event, "plugins_name", None),
        )
    except Exception as e:  # pragma: no cover - 防御性兜底
        logger.debug(f"[主动消息] 获取事件钩子列表失败喵: {e}")
        return False

    for handler in handlers or []:
        handler_name = getattr(handler, "handler_full_name", None) or getattr(
            handler, "handler_name", "unknown"
        )
        try:
            if not inspect.iscoroutinefunction(handler.handler):
                logger.warning(
                    f"[主动消息] 钩子 {handler_name} 不是协程函数，已跳过喵。"
                )
                continue
            await handler.handler(event, *args, **kwargs)
        except Exception as e:
            logger.error(
                f"[主动消息] 执行钩子失败喵！来源: {handler_name}, "
                f"错误类型: {type(e).__name__}, 错误详情: {e}\n"
                f"{traceback.format_exc()}"
            )

        try:
            if event.is_stopped():
                logger.info(f"[主动消息] 钩子 {handler_name} 终止了事件传播喵。")
                return True
        except Exception:
            continue

    try:
        return bool(event.is_stopped())
    except Exception:
        return False


class ProactiveMessageEvent(_EventBase):  # type: ignore[misc, valid-type]
    """主动消息专用的伪消息事件。

    主动消息没有真实来源事件，这里构造一个最小可用的伪事件，
    以便复用 AstrBot 的装饰钩子、发送能力与发送后钩子。
    """

    def __init__(
        self,
        *,
        plugin: Any,
        platform_meta: Any,
        session_id: str,
        target_id: str,
        message_type: MessageType,
        self_id: str,
        sender_id: str = "",
        sender_name: str = "",
        is_group: bool = False,
        persist_history: Any = None,
    ) -> None:
        # 注意：基类 __init__ 内部会读取 self.unified_msg_origin，
        # 因此这些被重写属性依赖的字段必须在 super().__init__() 之前完成赋值。
        self._proactive_plugin = plugin
        self._proactive_target_id = target_id
        self._proactive_umo = session_id
        self._proactive_is_group = is_group
        self._proactive_persist_history = persist_history
        self.proactive_sent_chains: list[MessageChain] = []
        self.proactive_send_failed = False

        message_obj = AstrBotMessage()
        message_obj.type = message_type
        message_obj.self_id = self_id or ""
        message_obj.session_id = target_id
        message_obj.message_id = f"{PROACTIVE_MESSAGE_ID_PREFIX}:{uuid.uuid4().hex}"
        message_obj.message = []
        message_obj.message_str = ""
        message_obj.raw_message = None
        resolved_sender = sender_id or ("" if is_group else target_id)
        message_obj.sender = MessageMember(
            user_id=str(resolved_sender), nickname=sender_name or None
        )
        if is_group:
            message_obj.group = Group(group_id=target_id)

        super().__init__(
            message_str="",
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=target_id,
        )

        self.plugins_name = None
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.role = "member"

    # ------------------------------------------------------------------
    # 会话标识
    # ------------------------------------------------------------------
    # 重写会话标识属性，使其始终指向构造时传入的 UMO。
    @property
    def unified_msg_origin(self) -> str:  # type: ignore[override]
        return self._proactive_umo

    @unified_msg_origin.setter
    def unified_msg_origin(self, value: str) -> None:  # type: ignore[override]
        self._proactive_umo = value

    # ------------------------------------------------------------------
    # 发送能力
    # ------------------------------------------------------------------
    async def send(self, message: MessageChain) -> bool:  # type: ignore[override]
        """发送消息：优先平台直发，失败回退核心 API。

        任一途径成功即补写平台流水并标记已发送；全部失败则记录失败标记。
        """
        if message is None:
            return False

        plugin = self._proactive_plugin
        if plugin is None:
            return False

        self.proactive_sent_chains.append(message)

        sent = False
        platform = self._resolve_platform()
        # 平台实例可用且运行中时优先直发。
        if platform is not None and platform.status == PlatformStatus.RUNNING:
            if MessageSession is None:  # pragma: no cover - 极旧版本
                sent = await self._send_via_core_api(message)
            else:
                try:
                    session_obj = MessageSession(
                        platform_name=platform.meta().id,
                        message_type=(
                            MessageType.GROUP_MESSAGE
                            if self._proactive_is_group
                            else MessageType.FRIEND_MESSAGE
                        ),
                        session_id=self._proactive_target_id,
                    )
                    await platform.send_by_session(session_obj, message)
                    sent = True
                except Exception as e:
                    logger.error(f"[主动消息] 平台发送失败喵，尝试核心 API 兜底: {e}")

        if not sent:
            sent = await self._send_via_core_api(message)

        if not sent:
            self.proactive_send_failed = True
            logger.error("[主动消息] 事件发送失败喵：平台与核心 API 均未能送达。")
            return False

        self._has_send_oper = True
        await self._persist_sent_chain(message)
        return True

    async def _send_via_core_api(self, message: MessageChain) -> bool:
        """通过核心发送 API 兜底发送。"""
        plugin = self._proactive_plugin
        if plugin is None:
            return False
        try:
            result = await plugin.context.send_message(self._proactive_umo, message)
        except Exception as e:  # pragma: no cover - 取决于运行时
            logger.error(f"[主动消息] 核心 API 发送失败喵: {e}")
            return False
        if result is False:
            logger.warning("[主动消息] 核心 API 未找到匹配平台，消息未送达喵。")
            return False
        return True

    async def _persist_sent_chain(self, message: MessageChain) -> None:
        """调用注入的回调补写平台流水（回调缺失时静默跳过）。"""
        callback = self._proactive_persist_history
        if callback is None:
            return
        try:
            await callback(self._proactive_umo, message)
        except Exception as e:  # pragma: no cover - 取决于运行时
            logger.warning(f"[主动消息] 补写平台流水失败喵: {e}")

    def _resolve_platform(self) -> Any:
        """按元数据 id/name 定位当前事件对应的平台实例。"""
        plugin = self._proactive_plugin
        if plugin is None:
            return None
        meta = getattr(self, "platform_meta", None)
        platform_id = getattr(meta, "id", None) or getattr(meta, "name", None)
        manager = getattr(getattr(plugin, "context", None), "platform_manager", None)
        return resolve_platform_instance(manager, platform_id)


def _resolve_persist_history_callback(plugin: Any) -> Any:
    """解析插件提供的平台流水补写回调（鸭子类型查找）。"""
    if plugin is None:
        return None
    sender_service = getattr(plugin, "sender_service", None)
    callback = getattr(
        sender_service, "persist_proactive_message_to_platform_history", None
    )
    return callback if callable(callback) else None


def build_proactive_event(
    *,
    plugin: Any,
    platform_inst: Any,
    session_id: str,
    target_id: str,
    msg_type_str: str,
    self_id: str = "",
    sender_id: str = "",
    sender_name: str = "",
    persist_history: Any = None,
) -> ProactiveMessageEvent | None:
    """构建主动消息伪事件。"""
    if AstrMessageEvent is None:  # pragma: no cover - 极旧版本
        logger.warning(
            "[主动消息] 当前 AstrBot 版本不支持构造事件对象，已跳过扩展钩子喵。"
        )
        return None

    if platform_inst is None:
        logger.debug("[主动消息] 未找到目标平台实例，事件将以降级模式构造喵。")

    platform_meta = (
        platform_inst.meta()
        if platform_inst is not None
        else _build_fallback_platform_meta(session_id)
    )

    if persist_history is None:
        persist_history = _resolve_persist_history_callback(plugin)

    is_group = is_group_umo(msg_type_str)
    try:
        return ProactiveMessageEvent(
            plugin=plugin,
            platform_meta=platform_meta,
            session_id=session_id,
            target_id=target_id,
            message_type=resolve_message_type(msg_type_str),
            self_id=self_id,
            sender_id=sender_id,
            sender_name=sender_name,
            is_group=is_group,
            persist_history=persist_history,
        )
    except Exception as e:  # pragma: no cover - 防御性兜底
        logger.error(f"[主动消息] 构造事件对象失败喵: {e}")
        return None


def _build_fallback_platform_meta(session_id: str) -> Any:
    """平台实例缺失时，按 UMO 前缀构造一个降级平台元数据。"""
    try:
        from astrbot.core.platform.platform_metadata import PlatformMetadata

        platform_id = session_id.split(":", 1)[0] if ":" in session_id else "default"
        return PlatformMetadata(name=platform_id, description="", id=platform_id)
    except Exception:  # pragma: no cover
        return None


def build_proactive_event_for_session(
    *,
    plugin: Any,
    session_id: str,
    persist_history: Any = None,
) -> ProactiveMessageEvent | None:
    """按 UMO 自治构建伪事件（自动解析平台、self_id 与发送者）。"""
    key = SessionKey.parse(session_id)
    if key is None:
        logger.debug(f"[主动消息] 无法解析会话标识，跳过事件构造喵: {session_id}")
        return None

    platform_id = key.platform
    msg_type_str = key.message_type
    target_id = key.target_id
    manager = getattr(getattr(plugin, "context", None), "platform_manager", None)
    platform_inst = resolve_platform_instance(manager, platform_id)
    self_id = resolve_self_id(plugin, session_id)
    sender_id, sender_name = resolve_sender_hint(plugin, session_id)

    return build_proactive_event(
        plugin=plugin,
        platform_inst=platform_inst,
        session_id=session_id,
        target_id=target_id,
        msg_type_str=msg_type_str,
        self_id=self_id,
        sender_id=sender_id,
        sender_name=sender_name,
        persist_history=persist_history,
    )


__all__ = [
    "PROACTIVE_MESSAGE_ID_PREFIX",
    "ProactiveMessageEvent",
    "build_proactive_event",
    "build_proactive_event_for_session",
    "dispatch_event_hook",
    "resolve_message_type",
    "resolve_platform_instance",
    "resolve_self_id",
    "resolve_sender_hint",
]
