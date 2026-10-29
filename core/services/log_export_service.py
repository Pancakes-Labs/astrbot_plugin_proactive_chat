"""运行日志导出服务。

把进程内 RuntimeLogCollector 捕获的插件运行日志行（INFO 及以上、仅 [主动消息] 相关），
按请求行数截取、整段脱敏后上传到自建 LogPaste 服务，返回可分享的链接。

设计取舍：
- 日志内容**不回传前端**，前端只拿到链接与统计信息，避免明文日志在浏览器/网络里多走一遍。
- 导出是管理员的显式动作，不随遥测开关联动（关闭遥测不影响导出）。
- 上传与脱敏都是阻塞型操作，统一放入线程池 / 复用异步会话，避免阻塞事件循环
  （预警推送、WebSocket 心跳共用同一循环）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from astrbot.api import logger

from ...utils.log_sanitizer import sanitize_log_text
from ...utils.runtime_log_collector import get_runtime_log_collector
from ...utils.version_utils import get_plugin_version
from ..remote.log_paste_client import format_expires_at, get_paste_client

# 日志导出行数边界与字节预算（自建 paste 服务端限制 2MB，预留头部与余量）。
LOG_EXPORT_DEFAULT_COUNT = 500
LOG_EXPORT_MAX_COUNT = 10000
LOG_EXPORT_MAX_BYTES = 1_900_000
# 运行日志导出的行过滤关键词：只导出本插件相关的运行日志行。
RUNTIME_LOG_EXPORT_KEYWORD = "[主动消息]"

# 两次成功上传之间的最小间隔（秒），防止误触/重复点击刷爆 paste 服务。
_MIN_UPLOAD_INTERVAL_SECONDS = 5


class LogExportService:
    """运行日志读取、脱敏与上传编排服务。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        # 导出动作串行化：同一时刻只允许一次导出，避免重复点击并发上传。
        self._lock = asyncio.Lock()
        # 上次成功上传的单调时间戳，用于最小间隔节流。
        self._last_success_at = 0.0

    # ------------------------------------------------------------------
    # 统计信息
    # ------------------------------------------------------------------
    def collect_stats(self) -> dict[str, Any]:
        """返回当前日志缓冲的统计信息（供前端展示可用行数与占用）。"""
        collector = get_runtime_log_collector()
        stats = collector.snapshot_stats()
        return {
            "installed": bool(collector.installed),
            "lines": int(stats.get("lines", 0)),
            "bytes": int(stats.get("bytes", 0)),
            "size_kb": round(int(stats.get("bytes", 0)) / 1024, 1),
            "default_count": LOG_EXPORT_DEFAULT_COUNT,
            "max_count": LOG_EXPORT_MAX_COUNT,
            "version": get_plugin_version(default="未知版本"),
        }

    @staticmethod
    def clamp_count(raw: Any) -> tuple[int, bool]:
        """解析并钳制请求行数，返回 (行数, 是否被钳制)。"""
        try:
            count = int(raw)
        except (TypeError, ValueError):
            count = LOG_EXPORT_DEFAULT_COUNT

        if count < 1:
            return 1, True
        if count > LOG_EXPORT_MAX_COUNT:
            return LOG_EXPORT_MAX_COUNT, True
        return count, False

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    async def export_snapshot(self, count: Any) -> tuple[str, int, bool]:
        """读取最近 N 行日志并拼装为脱敏后的导出文本。

        返回 (导出文本, 实际日志行数, 是否因字节预算被截断)。
        抛 RuntimeError("empty") 表示当前缓冲里没有任何可用日志行。
        """
        requested, _ = self.clamp_count(count)
        # 缓冲快照/过滤与整段脱敏均为 CPU 密集操作（文本量最大约 1.9MB），
        # 放入线程池执行，避免阻塞事件循环。
        lines, truncated_by_size = await asyncio.to_thread(
            get_runtime_log_collector().get_recent_lines,
            requested,
            keyword=RUNTIME_LOG_EXPORT_KEYWORD,
            max_total_bytes=LOG_EXPORT_MAX_BYTES,
        )
        if not lines:
            raise RuntimeError("empty")

        export_text = await asyncio.to_thread(
            self._build_export_text, lines, truncated_by_size
        )
        return export_text, len(lines), truncated_by_size

    @staticmethod
    def _build_export_text(lines: list[str], truncated_by_size: bool) -> str:
        """把运行日志行拼装为导出文本，并对整段内容脱敏。"""
        generated_at = (
            datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        )
        header = (
            "=== 主动消息插件运行日志导出 ===\n"
            f"导出时间: {generated_at}\n"
            f"插件版本: {get_plugin_version()}\n"
            f"日志行数: {len(lines)} 行（按时间升序，仅含 [主动消息] 相关日志）\n"
            f"大小截断: {'是（已达导出上限）' if truncated_by_size else '否'}\n"
            "说明: 以下为本进程自启动以来的插件运行日志，已对凭据、会话标识与本机路径脱敏。\n"
        )
        body = "\n".join(lines)
        return sanitize_log_text(f"{header}\n{body}\n")

    async def export_and_upload(self, count: Any = None) -> dict[str, Any]:
        """导出并上传，返回包含链接与统计的结果字典。

        失败时抛异常，由路由层转换为 HTTP 响应；成功时更新节流时间戳。
        """
        if self._lock.locked():
            raise RuntimeError("busy")
        if time.monotonic() - self._last_success_at < _MIN_UPLOAD_INTERVAL_SECONDS:
            raise RuntimeError("too_frequent")

        async with self._lock:
            requested, clamped = self.clamp_count(count)
            export_text, exported_lines, truncated_by_size = (
                await self.export_snapshot(requested)
            )
            payload = await get_paste_client().upload_text(export_text)

            self._last_success_at = time.monotonic()
            size_kb = round(len(export_text.encode("utf-8")) / 1024, 1)
            await self._track_result(
                {
                    "requested": requested,
                    "size_kb": size_kb,
                    "clamped": clamped,
                }
            )
            return {
                "ok": True,
                "url": str(payload.get("url") or ""),
                "raw_url": str(payload.get("raw_url") or ""),
                "expires_at": format_expires_at(payload.get("expires_at")),
                "requested": requested,
                "exported_lines": exported_lines,
                "size_kb": size_kb,
                "clamped": clamped,
                "truncated_by_size": truncated_by_size,
            }

    async def _track_result(self, extra: dict[str, Any]) -> None:
        """上报导出行为（遥测关闭时静默跳过，绝不影响导出结果）。"""
        telemetry = getattr(self.plugin, "telemetry", None)
        if not telemetry or not getattr(telemetry, "enabled", False):
            return
        data = {"action": "log_export", "success": True}
        data.update(extra)
        try:
            track_feature = getattr(telemetry, "track_feature", None)
            if not callable(track_feature):
                return
            self.plugin._track_task(
                asyncio.create_task(track_feature("command_admin_action", data))
            )
        except Exception as e:
            logger.debug(f"[主动消息] 日志导出遥测上报失败喵: {e}")


__all__ = [
    "LOG_EXPORT_DEFAULT_COUNT",
    "LOG_EXPORT_MAX_BYTES",
    "LOG_EXPORT_MAX_COUNT",
    "RUNTIME_LOG_EXPORT_KEYWORD",
    "LogExportService",
]
