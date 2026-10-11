"""会话标识值对象与判定函数。

本模块集中收敛全项目此前散落的 UMO 解析与群聊/私聊判定逻辑，
保证与既有实现逐字符等价。
"""

from __future__ import annotations

from dataclasses import dataclass

# 标准消息类型锚点。顺序与旧实现保持一致，避免同名平台/目标干扰匹配结果。
KNOWN_MESSAGE_TYPES: tuple[str, ...] = (
    "FriendMessage",
    "GroupMessage",
    "PrivateMessage",
    "GuildMessage",
)

# 会话类型段判定集合（用于按“消息类型段”精确判定，避免平台名干扰）。
_GROUP_TYPE_TOKENS = frozenset({"groupmessage", "guildmessage", "group", "guild"})
_PRIVATE_TYPE_TOKENS = frozenset(
    {"friendmessage", "privatemessage", "friend", "private"}
)


def is_group_umo(umo: str) -> bool:
    """判断 UMO 是否指向群聊会话。

    与旧版 ``proactive_event.is_group_session`` 行为完全一致：
    优先取“消息类型段”，仅在缺少类型段时退化为整串匹配。
    """
    text = (umo or "").strip()
    if not text:
        return False

    # segments[0] 是平台标识段，跳过后只看后续类型段。
    for segment in text.split(":")[1:]:
        lowered = segment.lower()
        if lowered in _GROUP_TYPE_TOKENS:
            return True
        if lowered in _PRIVATE_TYPE_TOKENS:
            return False

    # 缺少类型段的非标准 UMO：退化为整串匹配以保持向后兼容。
    lowered = text.lower()
    return "group" in lowered or "guild" in lowered


def is_friend_type(message_type: str) -> bool:
    """判断消息类型段是否属于私聊。"""
    return "Friend" in message_type or "Private" in message_type


def is_group_type(message_type: str) -> bool:
    """判断消息类型段是否属于群聊。"""
    return "Group" in message_type or "Guild" in message_type


def contains_group_marker(umo: str) -> bool:
    """粗粒度群聊判定：UMO 整串是否含 group。"""
    return "group" in (umo or "").lower()


@dataclass(frozen=True)
class SessionKey:
    """会话标识值对象：platform:message_type:target_id。"""

    platform: str
    message_type: str
    target_id: str

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @classmethod
    def parse(cls, umo: str) -> SessionKey | None:
        """解析 UMO 为 SessionKey，解析失败返回 None。

        与旧版 _parse_session_id 行为完全一致：
        先按标准类型锚点匹配，再兼容三段式与多段式 UMO。
        """
        if not isinstance(umo, str):
            return None

        # 先走锚点匹配，避免 platform/target 中包含冒号导致误切分。
        for msg_type in KNOWN_MESSAGE_TYPES:
            marker = f":{msg_type}:"
            idx = umo.find(marker)
            if idx != -1:
                platform = umo[:idx]
                after_type = umo[idx + len(marker) :]
                return cls(platform, msg_type, after_type)

        parts = umo.split(":")
        if len(parts) == 3:
            return cls(parts[0], parts[1], parts[2])
        if len(parts) > 3:
            return cls(":".join(parts[:-2]), parts[-2], parts[-1])
        return None

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    @property
    def is_group_message(self) -> bool:
        """按类型段判定群聊。"""
        return is_group_type(self.message_type)

    @property
    def is_friend_message(self) -> bool:
        """按类型段判定私聊。"""
        return is_friend_type(self.message_type)

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def __str__(self) -> str:
        return f"{self.platform}:{self.message_type}:{self.target_id}"
