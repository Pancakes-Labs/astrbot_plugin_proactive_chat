"""运行日志收集器。

在插件初始化时优先向 Loguru 挂载内存缓冲 Sink，持续记录本进程控制台运行日志行
（INFO 及以上，与控制台可见口径一致，只保留本插件 / [主动消息] 相关行），
供「Web 管理端 → 上传运行日志」读取最近 N 行并脱敏上传。

设计说明：
AstrBot 框架将所有插件及核心 Logger 均配置了 propagate=False，并通过
_LoguruInterceptHandler 统一重定向到 Loguru 输出控制台。若仅向标准库
Root Logger (logging.getLogger()) 挂载 Handler，会导致子 Logger 日志被隔离
而无法捕获。因此本收集器优先向 Loguru 注册内存 Sink，同时保留标准 logging
Handler 作为环境回退保障。
缓冲随进程存活，重启后从零重新累积。
"""

from __future__ import annotations

import logging
import re
import threading
import traceback
from collections import deque
from typing import Any

# ANSI 颜色转义字符过滤正则（清洗横幅及彩色日志，避免导出时出现乱码）。
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# 与 AstrBot 控制台可读性对齐的行格式（标准 logging 回退时使用）。
_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 缓冲默认容量（行）与捕获级别（INFO 及以上，与控制台可见口径一致）。
DEFAULT_MAX_LINES = 20000
DEFAULT_CAPTURE_LEVEL = logging.INFO

# 单条记录字节上限。内存预算不能只按行数算：_format_loguru_record 会把整段异常堆栈
# 拼进同一条记录，原始报文等可能让单条记录达数十 KB。
# 写入缓冲前按此上限截断（截断处追加标记，便于导出时识别）。
DEFAULT_MAX_RECORD_BYTES = 16 * 1024
# 缓冲总字节上限。即使单条被截断，行数 × 单条上限仍可达数百 MB，故再设整体预算，
# 超限时从最旧记录起逐出，保证常驻内存有确定上界（导出用的字节预算不限制缓冲本身）。
DEFAULT_MAX_TOTAL_BYTES = 8 * 1024 * 1024

# 本插件在 AstrBot 中的专用 logger 名（须与 AstrBot 命名约定保持一致）。
_PLUGIN_LOGGER_NAME = "astrbot.plugin.astrbot_plugin_proactive_chat"
# 插件标识片段与日志前缀，用于过滤无关插件/核心日志。
_PLUGIN_ID = "astrbot_plugin_proactive_chat"
_LOG_PREFIX = "[主动消息]"


class _RingBufferHandler(logging.Handler):
    """把格式化后的日志行写入有界环形缓冲（用于无 Loguru 时的回退）。"""

    def __init__(self, collector: RuntimeLogCollector, formatter: logging.Formatter):
        super().__init__()
        # 统一走收集器的 _append_line，复用单条截断与总字节预算逻辑。
        self._collector = collector
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        # 单条格式化失败直接吞掉，绝不影响宿主日志系统。
        try:
            line = self.format(record)
        except Exception:
            return
        if line:
            self._collector._append_line(line)


class RuntimeLogCollector:
    """运行日志内存收集器（进程内单例）。"""

    def __init__(
        self,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        capture_all: bool = False,
        max_record_bytes: int | None = DEFAULT_MAX_RECORD_BYTES,
        max_total_bytes: int | None = DEFAULT_MAX_TOTAL_BYTES,
    ):
        self._max_lines = max(1, max_lines)
        # 不设 maxlen，改由 _append_line 同时按行数与总字节逐出，保证两个预算都能精确核算。
        self._buffer: deque[str] = deque()
        self._total_bytes = 0
        self._max_record_bytes = (
            None
            if max_record_bytes is None or max_record_bytes <= 0
            else max_record_bytes
        )
        self._max_total_bytes = (
            None if max_total_bytes is None or max_total_bytes <= 0 else max_total_bytes
        )
        self._lock = threading.Lock()
        self._handler: _RingBufferHandler | None = None
        self._loguru_sink_id: int | None = None
        self._capture_all = capture_all

    @property
    def installed(self) -> bool:
        """是否已挂载到日志系统。"""
        return self._loguru_sink_id is not None or self._handler is not None

    def _filter_loguru_record(self, record: dict[str, Any]) -> bool:
        """只保留本插件相关日志，避免无关插件刷屏占满缓冲。"""
        if self._capture_all:
            return True
        extra = record.get("extra") or {}
        plugin_tag = str(extra.get("plugin_tag") or "")
        src_file = str(extra.get("source_file") or "")
        if _PLUGIN_ID in plugin_tag or _PLUGIN_ID in src_file:
            return True
        msg = str(record.get("message") or "")
        return _LOG_PREFIX in msg

    @staticmethod
    def _format_loguru_record(message: Any) -> str:
        """将 loguru 消息格式化为与控制台一致的单行日志字符串（含异常堆栈）。"""
        record = getattr(message, "record", None)
        if not record:
            return str(message).rstrip("\r\n")

        # 时间戳（毫秒精度）
        record_time = record.get("time")
        if hasattr(record_time, "strftime"):
            time_str = record_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        else:
            time_str = str(record_time)

        extra = record.get("extra") or {}
        plugin_tag = extra.get("plugin_tag", "")
        short_level = extra.get("short_levelname")
        if not short_level:
            level_obj = record.get("level")
            short_level = getattr(level_obj, "name", str(level_obj))
        version_tag = extra.get("astrbot_version_tag", "")
        src_file = extra.get("source_file")
        if not src_file:
            file_obj = record.get("file")
            src_file = getattr(file_obj, "name", "")
        src_line = extra.get("source_line")
        if src_line is None:
            src_line = record.get("line", "")

        msg = record.get("message", "")

        parts = [f"[{time_str}]"]
        if plugin_tag:
            tag_str = str(plugin_tag).strip()
            if not tag_str.startswith("["):
                tag_str = f"[{tag_str}]"
            parts.append(tag_str)
        level_str = str(short_level).strip()
        if not level_str.startswith("["):
            level_str = f"[{level_str}]"
        parts.append(level_str)
        if version_tag:
            v_str = str(version_tag).strip()
            if not v_str.startswith("["):
                v_str = f"[{v_str}]"
            parts.append(v_str)
        if src_file or src_line:
            parts.append(f"[{src_file}:{src_line}]:")

        prefix = " ".join(parts)
        formatted = f"{prefix} {msg}" if prefix else str(msg)

        exc = record.get("exception")
        if exc:
            try:
                exc_type, exc_val, exc_tb = exc
                tb_lines = "".join(
                    traceback.format_exception(exc_type, exc_val, exc_tb)
                )
                formatted = f"{formatted}\n{tb_lines.rstrip()}"
            except Exception:
                pass

        # 清洗可能存在的 ANSI 终端颜色转义字符（如彩色横幅），保证导出内容纯净易读。
        return _ANSI_ESCAPE_RE.sub("", formatted)

    def _append_line(self, line: str) -> None:
        """截断单条记录后写入环形缓冲，并按行数与总字节预算逐出最旧记录。"""
        line = self._truncate_record(line)
        if not line:
            return
        line_bytes = len(line.encode("utf-8"))
        with self._lock:
            self._buffer.append(line)
            self._total_bytes += line_bytes
            # 行数预算
            while len(self._buffer) > self._max_lines:
                self._total_bytes -= len(self._buffer.popleft().encode("utf-8"))
            # 总字节预算（至少保留 1 条，避免极度收紧时缓冲整段消失）
            if self._max_total_bytes is not None:
                while (
                    self._total_bytes > self._max_total_bytes and len(self._buffer) > 1
                ):
                    self._total_bytes -= len(self._buffer.popleft().encode("utf-8"))

    def _truncate_record(self, line: str) -> str:
        """按单条字节预算截断记录，超出部分丢弃并追加截断标记。"""
        if self._max_record_bytes is None:
            return line
        if len(line.encode("utf-8")) <= self._max_record_bytes:
            return line
        kept = self._cut_to_byte_budget(line, self._max_record_bytes)
        return f"{kept}…[单条日志超限已截断]"

    def _handle_loguru_message(self, message: Any) -> None:
        """Loguru Sink 回调：把格式化后的日志行存入环形缓冲。"""
        try:
            line = self._format_loguru_record(message)
            if not line:
                return
            self._append_line(line)
        except Exception:
            return

    def install(self, *, level: int = DEFAULT_CAPTURE_LEVEL) -> None:
        """挂载收集器（优先 Loguru，不可用时回退标准 logging，幂等）。"""
        self.uninstall()

        # 1. 尝试向 Loguru 注册 Sink（AstrBot 所有控制台日志的实际终点）
        try:
            from loguru import logger as loguru_logger

            level_name = (
                logging.getLevelName(level)
                if isinstance(level, int)
                else str(level).upper()
            )
            if not isinstance(level_name, str) or not level_name:
                level_name = "DEBUG"

            self._loguru_sink_id = loguru_logger.add(
                self._handle_loguru_message,
                level=level_name,
                filter=self._filter_loguru_record,
            )
            return
        except Exception:
            self._loguru_sink_id = None

        # 2. 回退机制：若无 Loguru，挂载到标准库 Root Logger 及插件 Logger
        handler = _RingBufferHandler(
            self, logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
        )
        handler.setLevel(level)
        logging.getLogger().addHandler(handler)
        try:
            logging.getLogger(_PLUGIN_LOGGER_NAME).addHandler(handler)
        except Exception:
            pass
        self._handler = handler

    def uninstall(self) -> None:
        """从日志系统移除并清空缓冲（幂等）。"""
        if self._loguru_sink_id is not None:
            try:
                from loguru import logger as loguru_logger

                loguru_logger.remove(self._loguru_sink_id)
            except Exception:
                pass
            self._loguru_sink_id = None

        if self._handler is not None:
            try:
                logging.getLogger().removeHandler(self._handler)
            except Exception:
                pass
            try:
                logging.getLogger(_PLUGIN_LOGGER_NAME).removeHandler(self._handler)
            except Exception:
                pass
            self._handler = None

        with self._lock:
            self._buffer.clear()
            self._total_bytes = 0

    def snapshot_stats(self) -> dict[str, int]:
        """返回缓冲的即时统计（行数与字节数），供前端展示。"""
        with self._lock:
            return {"lines": len(self._buffer), "bytes": self._total_bytes}

    def get_recent_lines(
        self,
        count: int,
        *,
        keyword: str | None = None,
        max_total_bytes: int | None = None,
    ) -> tuple[list[str], bool]:
        """读取最近的日志行（时间升序返回）。

        Args:
            count: 请求行数，从最新一行往回取。
            keyword: 可选行过滤关键词（如插件日志标记 [主动消息]），
                多行堆栈属于单条缓冲记录，会整块保留或整块丢弃。
            max_total_bytes: 可选总字节预算，达到预算后不再纳入更旧行；
                仅当首行就超预算时会就地截断该行，保证至少返回 1 行。

        Returns:
            (行列表, 是否因预算提前截断)
        """
        count = max(1, int(count))
        with self._lock:
            snapshot = list(self._buffer)

        matched = snapshot
        if keyword:
            matched = [
                line for line in snapshot if keyword in line or _PLUGIN_ID in line
            ]

        collected: list[str] = []  # 新行在前
        used_bytes = 0
        truncated_by_size = False
        for line in reversed(matched):
            line_bytes = len(line.encode("utf-8"))
            if max_total_bytes is not None and line_bytes > (
                max_total_bytes - used_bytes
            ):
                if not collected:
                    trimmed = self._cut_to_byte_budget(
                        line, max(0, max_total_bytes - used_bytes)
                    )
                    collected.append(trimmed)
                    used_bytes += len(trimmed.encode("utf-8"))
                truncated_by_size = True
                break
            collected.append(line)
            used_bytes += line_bytes
            if len(collected) >= count:
                break

        return list(reversed(collected)), truncated_by_size

    @staticmethod
    def _cut_to_byte_budget(text: str, max_bytes: int) -> str:
        """按 UTF-8 字节预算截断文本（忽略截断产生的残缺多字节字符）。"""
        if max_bytes <= 0:
            return ""
        raw = text.encode("utf-8")
        if len(raw) <= max_bytes:
            return text
        return raw[:max_bytes].decode("utf-8", errors="ignore")


# 进程内单例：插件初始化时安装，停机时卸载。
_runtime_log_collector: RuntimeLogCollector | None = None


def get_runtime_log_collector() -> RuntimeLogCollector:
    """获取全局运行日志收集器单例（惰性创建）。"""
    global _runtime_log_collector
    if _runtime_log_collector is None:
        _runtime_log_collector = RuntimeLogCollector()
    return _runtime_log_collector


def install_runtime_log_collector(
    *, level: int = DEFAULT_CAPTURE_LEVEL
) -> RuntimeLogCollector:
    """安装全局运行日志收集器（插件 initialize 时调用，幂等）。"""
    collector = get_runtime_log_collector()
    collector.install(level=level)
    return collector


def uninstall_runtime_log_collector() -> None:
    """卸载并清空全局运行日志收集器（插件停机时调用，幂等）。"""
    global _runtime_log_collector
    if _runtime_log_collector is not None:
        _runtime_log_collector.uninstall()


__all__ = [
    "DEFAULT_CAPTURE_LEVEL",
    "DEFAULT_MAX_LINES",
    "DEFAULT_MAX_RECORD_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "RuntimeLogCollector",
    "get_runtime_log_collector",
    "install_runtime_log_collector",
    "uninstall_runtime_log_collector",
]
