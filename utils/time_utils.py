"""时间工具函数模块。"""

from __future__ import annotations

import re
import zoneinfo
from datetime import datetime

# 中文星期名称，索引与 datetime.weekday() 对齐（0=周一）
_WEEKDAY_NAMES_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# 多时段分隔符：中英文逗号与顿号都接受，避免用户手输时因全角符号失效
_QUIET_RANGE_SEPARATOR = re.compile(r"[,，、]")


def parse_quiet_ranges(quiet_hours_str: str) -> list[tuple[int, int]]:
    """把免打扰配置解析成小时区间列表。

    支持多时段，分隔符可以是英文逗号、中文逗号或顿号，
    例如 "1-7, 12-14，18-20、23-7"。
    end 允许取 24，表示到当天 24:00；start == end 视为空区间直接忽略。
    非法片段会被静默跳过，避免一处写错导致整段配置失效。
    """
    ranges: list[tuple[int, int]] = []
    for chunk in _QUIET_RANGE_SEPARATOR.split(str(quiet_hours_str)):
        part = chunk.strip()
        if not part:
            continue
        try:
            start_str, end_str = part.split("-")
            start_hour, end_hour = int(start_str), int(end_str)
        except (ValueError, TypeError):
            continue
        # 小时只允许 0-23 起始，0-24 结束
        if not (0 <= start_hour <= 23 and 0 <= end_hour <= 24):
            continue
        if start_hour == end_hour:
            continue
        ranges.append((start_hour, end_hour))
    return ranges


def is_quiet_time(quiet_hours_str: str, tz: zoneinfo.ZoneInfo | None) -> bool:
    """检查当前时间是否落在任免打扰时段内。"""
    ranges = parse_quiet_ranges(quiet_hours_str)
    if not ranges:
        return False

    # 若未提供时区则使用系统本地时间
    now = datetime.now(tz) if tz else datetime.now()
    hour = now.hour
    for start_hour, end_hour in ranges:
        if start_hour < end_hour:
            # 同日区间，例如 1-7；end=24 时覆盖到当天结束
            if start_hour <= hour < end_hour:
                return True
        else:
            # 跨日区间，例如 23-6
            if hour >= start_hour or hour < end_hour:
                return True
    return False


def format_current_time(tz: zoneinfo.ZoneInfo | None = None) -> str:
    """返回带中文星期的时间字符串，例如 2026年05月12日 周二 09:53。

    星期信息直接由 datetime 计算得出，避免模型自行由日期反推星期几而算错。
    """
    now = datetime.now(tz) if tz else datetime.now()
    weekday = _WEEKDAY_NAMES_ZH[now.weekday()]
    return f"{now.strftime('%Y年%m月%d日')} {weekday} {now.strftime('%H:%M')}"
