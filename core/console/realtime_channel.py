"""Web 控制台实时推送通道。

由旧 core/web_admin_server.py 的 WebSocket 广播逻辑等价搬迁而来。
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - 取决于是否安装 FastAPI
    from fastapi import WebSocket
except ImportError:  # pragma: no cover
    WebSocket = Any  # type: ignore[assignment,misc]


class RealtimeChannel:
    """管理 WebSocket 连接与广播。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._ws_connections: list[WebSocket] = []

    @property
    def connections(self) -> list[WebSocket]:
        return self._ws_connections

    @property
    def connection_count(self) -> int:
        return len(self._ws_connections)

    def add(self, websocket: WebSocket) -> None:
        self._ws_connections.append(websocket)

    def remove(self, websocket: WebSocket) -> None:
        if websocket in self._ws_connections:
            self._ws_connections.remove(websocket)

    def clear(self) -> None:
        self._ws_connections.clear()

    async def broadcast_ws_payload(self, payload: dict[str, Any]) -> None:
        to_remove: list[WebSocket] = []
        for ws in list(self._ws_connections):
            try:
                await ws.send_json(payload)
            except Exception:
                to_remove.append(ws)

        for ws in to_remove:
            if ws in self._ws_connections:
                self._ws_connections.remove(ws)
