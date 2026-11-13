"""会话标识值对象与判定函数。

本模块集中收敛全项目此前散落的 UMO 解析与群聊/私聊判定逻辑，
保证与既有实现逐字符等价。
"""

from __future__ import annotations

from dataclasses import dataclass

# AstrBot 核心 MessageType 的全部取值，见 astrbot.core.platform.message_type。
# 除此之外的类型段都不存在，不要凭想象扩表。
KNOWN_MESSAGE_TYPES: tuple[str, ...] = (
    "GroupMessage",
    "FriendMessage",
    "OtherMessage",
)


def is_group_umo(umo: str) -> bool:
    """判断 UMO 是否指向群聊会话。

    只看第二段消息类型，与 AstrBot 的 UMO 格式严格对齐。
    """
    parts = (umo or "").split(":")
    if len(parts) < 2:
        return False
    return parts[1] == "GroupMessage"


def is_friend_type(message_type: str) -> bool:
    """判断消息类型段是否属于私聊。"""
    return message_type == "FriendMessage"


def is_group_type(message_type: str) -> bool:
    """判断消息类型段是否属于群聊。"""
    return message_type == "GroupMessage"


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

        UMO 固定为 platform:message_type:target_id。平台段可能自带冒号，
        因此按真实类型锚点定位切分点，不靠冒号数量猜测。
        """
        if not isinstance(umo, str):
            return None

        for msg_type in KNOWN_MESSAGE_TYPES:
            marker = f":{msg_type}:"
            idx = umo.find(marker)
            if idx != -1:
                platform = umo[:idx]
                after_type = umo[idx + len(marker) :]
                if platform and after_type:
                    return cls(platform, msg_type, after_type)

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
