"""Paste 上传客户端。

封装自建 LogPaste 服务的纯文本上传契约：
POST 纯文本（text/plain; charset=utf-8）→ 201 返回 JSON（url / raw_url / expires_at）。

供 Web 管理端「上传运行日志」链路使用。
上传端点由开发者定死为常量，不作为用户配置项暴露。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import aiohttp

from astrbot.api import logger

# 自建 LogPaste 服务上传端点。
PASTE_ENDPOINT = "https://paste.aloys23.link/api/v1/pastes"

# 单次上传总超时（秒）。
_UPLOAD_TIMEOUT_SECONDS = 20


class PasteUploadError(Exception):
    """Paste 上传失败（网络异常、超时或服务端返回非 201）。"""


class PasteClient:
    """自建 LogPaste 服务纯文本上传客户端。"""

    def __init__(self, endpoint: str = PASTE_ENDPOINT):
        self._endpoint = endpoint
        # aiohttp 会话懒加载，避免无上传需求时占用连接资源。
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建内部网络会话（并发安全）。"""
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._lock:
            # 双重检查：等待锁期间可能已被其它协程创建。
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=_UPLOAD_TIMEOUT_SECONDS)
                self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def upload_text(self, text: str) -> dict:
        """上传纯文本并返回服务端 JSON（url / raw_url / expires_at）。

        失败统一抛出 PasteUploadError，由调用方决定提示与日志策略。
        """
        if not text or not text.strip():
            raise PasteUploadError("上传内容为空")

        session = await self._get_session()
        try:
            async with session.post(
                self._endpoint,
                data=text.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
            ) as resp:
                status = resp.status
                raw_body = await resp.text()
                if status != 201:
                    raise PasteUploadError(
                        f"服务端返回 HTTP {status}: {raw_body[:200]}"
                    )
                try:
                    payload = json.loads(raw_body)
                except ValueError as e:
                    raise PasteUploadError(f"响应 JSON 解析失败: {e}") from e
                if not isinstance(payload, dict) or not payload.get("url"):
                    raise PasteUploadError("响应缺少 url 字段")
                return payload
        except aiohttp.ClientError as e:
            raise PasteUploadError(f"网络请求失败: {e}") from e
        except asyncio.TimeoutError as e:
            raise PasteUploadError("请求超时") from e

    async def close(self) -> None:
        """关闭内部网络会话（幂等）。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def format_expires_at(expires_at: Any) -> str:
    """把服务端返回的过期时间 ISO 字符串转为本地时区可读文本。

    解析失败时原样返回，保证调用方展示不中断。
    """
    if not expires_at or not isinstance(expires_at, str):
        return str(expires_at or "未知")
    try:
        # Python 3.10 的 fromisoformat 不识别 Z 后缀，先归一化为 +00:00。
        dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return expires_at


# 模块级单例：paste 客户端无状态依赖（端点为常量），可安全惰性创建，
# 保证「上传运行日志」等显式动作在任何装配状态下都可用。
_paste_client: PasteClient | None = None


def get_paste_client() -> PasteClient:
    """获取全局 paste 客户端单例（未初始化时按默认端点惰性创建）。"""
    global _paste_client
    if _paste_client is None:
        _paste_client = PasteClient()
        logger.debug("[主动消息] 已创建 Paste 上传客户端喵。")
    return _paste_client


async def close_paste_client() -> None:
    """关闭并清空全局 paste 客户端单例（幂等）。"""
    global _paste_client
    if _paste_client is not None:
        try:
            await _paste_client.close()
        except Exception as e:
            logger.debug(f"[主动消息] 关闭 Paste 客户端会话失败（已忽略）喵: {e}")
        _paste_client = None


__all__ = [
    "PASTE_ENDPOINT",
    "PasteClient",
    "PasteUploadError",
    "close_paste_client",
    "format_expires_at",
    "get_paste_client",
]
