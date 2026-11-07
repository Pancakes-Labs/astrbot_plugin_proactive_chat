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


# 发送结果：全部送达 / 部分送达 / 完全未送达。
# 部分送达必须单独区分，否则调用方会重发全文，接收方看到重复内容。
FORWARD_SENT = "sent"
FORWARD_PARTIAL = "partial"
FORWARD_FAILED = "failed"


async def send_forward_blocks(
    plugin: Any,
    event: Any,
    blocks: list[str],
    *,
    header: str | None = None,
    name: str = FORWARD_NAME,
    max_nodes: int = MAX_NODES_PER_FORWARD,
) -> str:
    """把文本块作为合并转发发送到当前会话。

    Returns:
        FORWARD_SENT：全部批次送达；
        FORWARD_PARTIAL：至少一批已送达但后续失败，调用方不得重发全文；
        FORWARD_FAILED：完全未送达，调用方可回退纯文本。
    """
    if not blocks and not header:
        return FORWARD_FAILED

    # 合并转发是主动发送，若后续不再 yield 结果会误触发 LLM 回退，
    # 因此发送前先标记已处理。
    try:
        event.should_call_llm(True)
    except Exception as exc:  # noqa: BLE001 - 标记失败不影响发送
        logger.debug(f"[主动消息] 标记阻止默认 LLM 请求失败喵: {exc}")

    total = len(blocks)
    sent_batches = 0
    try:
        batches: list[Any] = []
        if total == 0:
            nodes = build_forward_nodes([], event=event, header=header, name=name)
            if nodes is not None:
                batches.append(nodes)
        else:
            index = 0
            first_batch = True
            while index < total:
                # 首批要给 header 预留一个节点，否则满额区块加标题会超限被拒。
                size = max(1, max_nodes - 1) if (first_batch and header) else max_nodes
                chunk = blocks[index : index + size]
                nodes = build_forward_nodes(
                    chunk,
                    event=event,
                    header=header if first_batch else None,
                    name=name,
                )
                if nodes is not None:
                    batches.append(nodes)
                index += size
                first_batch = False

        if not batches:
            return FORWARD_FAILED

        for nodes in batches:
            # 核心发送返回 False 表示找不到匹配平台，属于未送达，必须感知。
            result = await plugin.context.send_message(
                event.unified_msg_origin, MessageChain([nodes])
            )
            if result is False:
                logger.warning("[主动消息] 合并转发未能找到匹配平台，消息未送达喵。")
                return FORWARD_PARTIAL if sent_batches else FORWARD_FAILED
            sent_batches += 1
    except Exception as exc:  # noqa: BLE001 - 已送达部分不能重发
        logger.warning(f"[主动消息] 合并转发发送失败喵: {exc}")
        return FORWARD_PARTIAL if sent_batches else FORWARD_FAILED

    logger.debug(f"[主动消息] 合并转发已发送 {sent_batches} 批 / 共 {total} 块喵。")
    return FORWARD_SENT


__all__ = [
    "FORWARD_FAILED",
    "FORWARD_NAME",
    "FORWARD_PARTIAL",
    "FORWARD_SENT",
    "MAX_NODES_PER_FORWARD",
    "build_forward_nodes",
    "send_forward_blocks",
]
