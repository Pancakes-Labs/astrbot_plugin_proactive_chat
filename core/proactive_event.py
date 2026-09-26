"""主动消息事件基础设施。

本模块提供两项通用能力，供任何需要“凭空构造消息事件”的场景复用：

1. 继承真实事件基类的事件对象，具备完整字段与真实发送能力，可直接承载 AstrBot 的标准钩子。
2. 标准事件钩子派发器，行为与官方 call_event_hook 对齐，并额外提供旧版本兼容与异常容错。

之所以需要这套基础设施：主动消息并非由平台下发，而是插件主动触发，
因此没有天然的事件对象。若只是“伪造一个最小对象”，会导致依赖
plugins_name、is_wake、result_content_type、event.send() 等字段的
第三方插件静默失效。本模块把这些字段一次性补齐，使主动消息与普通聊天在扩展生态中行为一致。

关键设计约定：

- 单一事件贯穿全链路：第三方插件会在 LLM 阶段通过 event.set_extra(...)
  写入状态，并在装饰阶段读取。整条链路必须复用同一个事件实例。
- 发送能力必须真实：部分插件在装饰阶段用 event.send(...) 补发消息，
  基类实现只做指标上报，因此这里显式委托给平台实例。
- 字段与官方对齐：plugins_name=None 表示对所有插件生效，与官方语义一致。
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


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------
def is_group_session(umo: str) -> bool:
    """判断 UMO 是否指向群聊会话。"""
    lowered = (umo or "").lower()
    return "group" in lowered or "guild" in lowered


def resolve_message_type(umo: str) -> MessageType:
    """根据 UMO 推断标准 MessageType。"""
    return (
        MessageType.GROUP_MESSAGE if is_group_session(umo) else MessageType.FRIEND_MESSAGE
    )


def resolve_platform_instance(
    platform_manager: Any,
    platform_id: str,
) -> Any:
    """按平台 ID 解析平台实例，匹配不到时退回按平台显示名匹配。

    同时兼容 meta().id 与 meta().name 两种标识方式，
    避免不同版本/不同适配器的命名差异导致解析失败。

    Args:
        platform_manager: context.platform_manager。
        platform_id: UMO 中的平台标识。

    Returns:
        匹配到的平台实例；未命中返回 None。
    """
    if platform_manager is None or not platform_id:
        return None

    try:
        platforms = platform_manager.get_insts()
    except Exception:
        try:
            platforms = platform_manager.platform_insts
        except Exception:
            return None

    if not platforms:
        return None

    for inst in platforms:
        try:
            if inst.meta().id == platform_id:
                return inst
        except Exception:
            continue

    for inst in platforms:
        try:
            if inst.meta().name == platform_id:
                return inst
        except Exception:
            continue

    return None


def resolve_self_id(plugin: Any, umo: str) -> str:
    """解析机器人自身 ID。

    优先读取该会话已持久化的 self_id，缺失时回退会话配置中的平台信息，
    最后才退化为占位符，避免把空值传给依赖 self_id 的插件。
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
    """解析主动消息的发送者占位信息。

    主动消息没有真实发送者。群聊场景下，把最近一次活跃用户作为发送者，
    可以让依赖 get_sender_id() / get_sender_name() 的插件获得合理取值；
    私聊场景则回退为会话目标本身。

    Returns:
        (sender_id, sender_name)
    """
    session_data = getattr(plugin, "session_data", None)
    payload = session_data.get(umo) if isinstance(session_data, dict) else None
    if isinstance(payload, dict):
        sender_id = str(payload.get("last_sender_id") or "").strip()
        sender_name = str(payload.get("last_sender_name") or "").strip()
        if sender_id or sender_name:
            return sender_id, sender_name
    return "", ""


# ----------------------------------------------------------------------
# 通用钩子派发
# ----------------------------------------------------------------------
async def dispatch_event_hook(
    event: Any,
    hook_type: Any,
    *args: Any,
    **kwargs: Any,
) -> bool:
    """派发 AstrBot 标准事件钩子。

    该函数是通用能力，行为与官方 call_event_hook 保持一致：

    - 按 event.plugins_name 过滤可参与的插件；
    - 逐个 await 钩子处理函数；
    - 任一钩子终止事件传播时立即返回 True。

    相比官方实现，这里额外做了两点加固：

    - 钩子执行异常不会中断整条链路（官方仅打日志，这里同样隔离到单个钩子）；
    - 旧版本 AstrBot 缺少注册表时安全跳过，而不是抛异常。

    Args:
        event: 事件对象，需提供 plugins_name 与 is_stopped()。
        hook_type: `EventType` 枚举值。
        *args: 透传给钩子处理函数的额外位置参数。
        **kwargs: 透传给钩子处理函数的额外关键字参数。

    Returns:
        True 表示事件已被终止，调用方应放弃后续流程。
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
                # 官方通过 assert 强制要求异步钩子；这里宽容跳过并提示，
                # 避免第三方插件的同步钩子直接打断整条主动消息链路。
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
                logger.info(
                    f"[主动消息] 钩子 {handler_name} 终止了事件传播喵。"
                )
                return True
        except Exception:
            continue

    try:
        return bool(event.is_stopped())
    except Exception:
        return False


# ----------------------------------------------------------------------
# 伪事件
# ----------------------------------------------------------------------
class ProactiveMessageEvent(_EventBase):  # type: ignore[misc, valid-type]
    """主动消息专用的伪消息事件。

    该事件在主动消息的完整生命周期中复用，承载 LLM 钩子与装饰钩子的上下文。
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
    ) -> None:
        # 注意：基类 __init__ 内部会读取 self.unified_msg_origin（用于 TraceSpan），
        # 因此这些被重写属性依赖的字段必须在 super().__init__() 之前完成赋值。
        self._proactive_plugin = plugin
        self._proactive_target_id = target_id
        self._proactive_umo = session_id
        self._proactive_is_group = is_group
        self.proactive_sent_chains: list[MessageChain] = []

        message_obj = AstrBotMessage()
        message_obj.type = message_type
        message_obj.self_id = self_id or ""
        message_obj.session_id = target_id
        message_obj.message_id = f"{PROACTIVE_MESSAGE_ID_PREFIX}:{uuid.uuid4().hex}"
        message_obj.message = []
        message_obj.message_str = ""
        message_obj.raw_message = None
        # 主动消息没有真实发送者：群聊用最近活跃成员，私聊用会话目标本身。
        # 绝不能用群号冒充用户 ID，否则依赖发送者身份的插件会误判。
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

        # 对齐官方 pipeline 构造的事件字段，避免依赖方静默失效。
        # `plugins_name=None` 表示对所有插件生效，与官方语义一致。
        self.plugins_name = None
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.role = "member"

    # ------------------------------------------------------------------
    # 会话标识
    # ------------------------------------------------------------------
    @property
    def unified_msg_origin(self) -> str:  # type: ignore[override]
        """统一消息来源，保持与真实事件一致的 UMO 语义。"""
        return self._proactive_umo

    @unified_msg_origin.setter
    def unified_msg_origin(self, value: str) -> None:  # type: ignore[override]
        self._proactive_umo = value

    # ------------------------------------------------------------------
    # 发送能力
    # ------------------------------------------------------------------
    async def send(self, message: MessageChain) -> None:  # type: ignore[override]
        """发送消息到目标平台。

        显式委托给平台实例，并在平台不可用时回退到核心发送 API。
        """
        if message is None:
            return

        self.proactive_sent_chains.append(message)
        self._has_send_oper = True

        plugin = self._proactive_plugin
        if plugin is None:
            return

        platform = self._resolve_platform()
        if platform is not None and platform.status == PlatformStatus.RUNNING:
            if MessageSession is None:  # pragma: no cover - 极旧版本
                await self._send_via_core_api(message)
                return
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
                return
            except Exception as e:
                logger.error(f"[主动消息] 平台发送失败喵，尝试核心 API 兜底: {e}")

        await self._send_via_core_api(message)

    async def _send_via_core_api(self, message: MessageChain) -> None:
        """通过核心发送 API 兜底发送。"""
        plugin = self._proactive_plugin
        if plugin is None:
            return
        try:
            await plugin.context.send_message(self._proactive_umo, message)
        except Exception as e:  # pragma: no cover - 取决于运行时
            logger.error(f"[主动消息] 核心 API 发送失败喵: {e}")

    def _resolve_platform(self) -> Any:
        """解析当前事件所属的平台实例。"""
        plugin = self._proactive_plugin
        if plugin is None:
            return None
        meta = getattr(self, "platform_meta", None)
        platform_id = getattr(meta, "id", None) or getattr(meta, "name", None)
        manager = getattr(getattr(plugin, "context", None), "platform_manager", None)
        return resolve_platform_instance(manager, platform_id)


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
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
) -> ProactiveMessageEvent | None:
    """构建主动消息伪事件。

    Args:
        plugin: 插件实例，用于访问 context 与持久化状态。
        platform_inst: 目标平台实例；为 None 时仍会构造事件，但发送能力降级。
        session_id: 规范化后的完整 UMO。
        target_id: UMO 中的会话目标 ID。
        msg_type_str: UMO 中的消息类型字符串。
        self_id: 机器人自身 ID。
        sender_id: 最近活跃的发送者 ID（群聊场景）。
        sender_name: 最近活跃的发送者昵称。

    Returns:
        构造好的伪事件；基类不可用时返回 None。
    """
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

    is_group = is_group_session(msg_type_str)
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
        )
    except Exception as e:  # pragma: no cover - 防御性兜底
        logger.error(f"[主动消息] 构造事件对象失败喵: {e}")
        return None


def _build_fallback_platform_meta(session_id: str) -> Any:
    """在平台实例缺失时构造占位元数据。"""
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
) -> ProactiveMessageEvent | None:
    """按 UMO 自治构建伪事件（自动解析平台、self_id 与发送者）。

    这是推荐入口：调用方只需提供插件实例与 UMO，
    其余字段由本函数根据运行时状态推断。
    """
    parse_session_id = getattr(plugin, "_parse_session_id", None)
    parsed = parse_session_id(session_id) if callable(parse_session_id) else None
    if not parsed or len(parsed) != 3:
        logger.debug(f"[主动消息] 无法解析会话标识，跳过事件构造喵: {session_id}")
        return None

    platform_id, msg_type_str, target_id = parsed
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
    )


__all__ = [
    "PROACTIVE_MESSAGE_ID_PREFIX",
    "ProactiveMessageEvent",
    "build_proactive_event",
    "build_proactive_event_for_session",
    "dispatch_event_hook",
    "is_group_session",
    "resolve_message_type",
    "resolve_platform_instance",
    "resolve_self_id",
    "resolve_sender_hint",
]
