"""管理员指令处理。

承载运行日志相关的聊天指令（上传 / 统计），与 Web 端共用同一个
LogExportService，保证两条链路的行数钳制、脱敏口径与节流策略完全一致。

指令结果只回复链接近似内容，不把日志正文发到聊天里。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..services.log_export_service import (
    LOG_EXPORT_DEFAULT_COUNT,
    LOG_EXPORT_MAX_COUNT,
    LogExportService,
)

USAGE_TEXT = (
    "📌 用法：\n"
    "/主动消息日志上传 [行数]  — 上传最近运行日志并返回分享链接\n"
    "/主动消息日志          — 查看当前可导出的运行日志概况\n"
    f"💡 行数范围 1~{LOG_EXPORT_MAX_COUNT}，默认 {LOG_EXPORT_DEFAULT_COUNT}"
)


class CommandHandlers:
    """管理员聊天指令处理逻辑。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @property
    def log_service(self) -> LogExportService | None:
        """获取日志导出服务（未装配时返回 None）。"""
        return getattr(self.plugin, "log_export_service", None)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    async def _ensure_admin(event: AstrMessageEvent) -> bool:
        """指令级权限兜底（装饰器已限制，这里防御多平台实现差异）。"""
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin) and is_admin():
            return True
        return False

    # ------------------------------------------------------------------
    # 指令：日志概况
    # ------------------------------------------------------------------
    async def show_log_stats(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[Any, None]:
        """回复当前运行日志缓冲的统计信息。"""
        if not await self._ensure_admin(event):
            yield event.plain_result("🚫 权限不足：此指令仅限管理员使用。")
            return

        service = self.log_service
        if service is None:
            yield event.plain_result("❌ 日志服务不可用，请重载插件后再试。")
            return

        try:
            stats = await asyncio.to_thread(service.collect_stats)
        except Exception as e:
            logger.error(f"[主动消息] 获取日志统计失败喵: {e}")
            yield event.plain_result(f"❌ 获取日志统计失败: {e}")
            return

        lines = int(stats.get("lines", 0))
        if lines <= 0:
            yield event.plain_result(
                "📋 暂无运行日志记录\n\n运行日志自插件本次启动开始累积，稍后再试。"
            )
            return

        yield event.plain_result(
            "📊 运行日志概况\n\n"
            f"📄 可导出行数：{lines} 行\n"
            f"📦 缓冲占用：{stats.get('size_kb', 0)} KB\n"
            f"🔍 采集范围：INFO 及以上，仅 [主动消息] 相关日志\n"
            f"🔗 上传指令：/主动消息日志上传 [行数]\n"
            f"💡 行数范围 1~{LOG_EXPORT_MAX_COUNT}，默认 {LOG_EXPORT_DEFAULT_COUNT}"
        )

    # ------------------------------------------------------------------
    # 指令：上传日志
    # ------------------------------------------------------------------
    async def upload_logs(
        self, event: AstrMessageEvent, count: Any = None
    ) -> AsyncGenerator[Any, None]:
        """上传最近运行日志并回复分享链接。"""
        if not await self._ensure_admin(event):
            yield event.plain_result("🚫 权限不足：此指令仅限管理员使用。")
            return

        service = self.log_service
        if service is None:
            yield event.plain_result("❌ 日志服务不可用，请重载插件后再试。")
            return

        # 参数校验：指令参数解析器可能把纯数字转成 int，这里统一兜底。
        requested = LOG_EXPORT_DEFAULT_COUNT
        if count is not None and str(count).strip():
            try:
                requested = int(str(count).strip())
            except ValueError:
                yield event.plain_result(f"❌ 行数参数无效。\n\n{USAGE_TEXT}")
                return

        requested, clamped = service.clamp_count(requested)

        yield event.plain_result(
            f"📜 正在读取并上传最近 {requested} 行运行日志，请稍候…"
        )

        try:
            result = await service.export_and_upload(requested)
        except RuntimeError as e:
            reason = str(e)
            if reason == "empty":
                yield event.plain_result(
                    "📋 暂无运行日志记录\n\n运行日志自插件本次启动开始累积，稍后再试。"
                )
                return
            if reason == "busy":
                yield event.plain_result("⏳ 已有日志上传正在进行，请等待完成后再试。")
                return
            if reason == "too_frequent":
                yield event.plain_result("⏳ 上传过于频繁，请等待几秒后再试。")
                return
            logger.warning(f"[主动消息] 日志上传失败喵: {e}")
            yield event.plain_result(f"❌ 日志上传失败: {reason}")
            return
        except Exception as e:
            # 失败细节留在服务端日志，聊天里只回可读原因。
            logger.warning(f"[主动消息] 日志上传失败喵: {e}")
            yield event.plain_result(f"❌ 日志上传失败: {e}")
            return

        lines_out = [
            "✅ 运行日志上传成功（内容已脱敏）",
            f"📦 行数：{result.get('exported_lines', 0)} 行（{result.get('size_kb', 0)} KB）",
            f"🔗 链接：{result.get('url', '')}",
        ]
        expires_at = result.get("expires_at")
        if expires_at and expires_at != "未知":
            lines_out.append(f"⏳ 过期时间：{expires_at}")
        if clamped:
            lines_out.append(f"ℹ️ 行数已钳制到允许范围 1~{LOG_EXPORT_MAX_COUNT}")
        if result.get("truncated_by_size"):
            lines_out.append("⚠️ 已达到上传大小上限，仅包含最新可容纳的日志行")
        elif int(result.get("exported_lines", 0)) < requested:
            lines_out.append(
                f"ℹ️ 当前缓冲共 {result.get('exported_lines', 0)} 行，"
                f"不足请求的 {requested} 行"
            )
        lines_out.append("🔒 提示：链接可被任何拿到它的人访问，请确认后再公开分享。")
        yield event.plain_result("\n".join(lines_out))


__all__ = ["CommandHandlers"]
