"""合并转发消息构建辅助。

指令输出的信息密度较高（尤其是全局状态），单条文本消息容易刷屏，
因此统一走合并转发；平台不支持时由调用方回退为纯文本。
"""

from __future__ import annotations

from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain

# 单条合并转发最多节点数，超出则分批发送。
MAX_NODES_PER_FORWARD = 10
# 合并转发中显示的发送者名称。
FORWARD_NAME = "主动消息"


def build_forward_nodes(
    blocks: list[str],
    *,
    event: Any,
    header: str | None = None,
    name: str = FORWARD_NAME,
) -> Any:
    """把文本块打包成 Comp.Nodes；无内容时返回 None。"""
    if not blocks and not header:
        return None

    try:
        bot_id = event.get_self_id() or "0"
    except Exception:
        bot_id = "0"

    nodes = Comp.Nodes([])

    def _append(text: str) -> None:
        content = str(text or "").strip()
        if not content:
            return
        nodes.nodes.append(
            Comp.Node(uin=bot_id, name=name, content=[Comp.Plain(content)])
        )

    if header:
        _append(header)
    for block in blocks:
        _append(block)

    if not nodes.nodes:
        return None
    return nodes


async def send_forward_blocks(
    plugin: Any,
    event: Any,
    blocks: list[str],
    *,
    header: str | None = None,
    name: str = FORWARD_NAME,
    max_nodes: int = MAX_NODES_PER_FORWARD,
) -> bool:
    """把文本块作为合并转发发送到当前会话。

    Returns:
        True 表示已成功走合并转发；False 表示无内容或发送失败，
        调用方应自行回退纯文本。
    """
    if not blocks and not header:
        return False

    # 合并转发是主动发送，若后续不再 yield 结果会误触发 LLM 回退，
    # 因此发送前先标记已处理。
    try:
        event.should_call_llm(True)
    except Exception as exc:  # noqa: BLE001 - 标记失败不影响发送
        logger.debug(f"[主动消息] 标记阻止默认 LLM 请求失败喵: {exc}")

    total = len(blocks)
    try:
        # 首批带 header，后续批次不带，避免头部重复。
        batches: list[Any] = []
        if total == 0:
            nodes = build_forward_nodes([], event=event, header=header, name=name)
            if nodes is not None:
                batches.append(nodes)
        for start in range(0, total, max_nodes):
            batch_blocks = blocks[start : start + max_nodes]
            nodes = build_forward_nodes(
                batch_blocks,
                event=event,
                header=header if start == 0 else None,
                name=name,
            )
            if nodes is not None:
                batches.append(nodes)

        if not batches:
            return False

        for nodes in batches:
            await plugin.context.send_message(
                event.unified_msg_origin, MessageChain([nodes])
            )
    except Exception as exc:  # noqa: BLE001 - 交给调用方回退纯文本
        logger.warning(f"[主动消息] 合并转发发送失败喵: {exc}")
        return False

    logger.debug(f"[主动消息] 合并转发已发送 {len(batches)} 批 / 共 {total} 块喵。")
    return True


__all__ = [
    "FORWARD_NAME",
    "MAX_NODES_PER_FORWARD",
    "build_forward_nodes",
    "send_forward_blocks",
]
