"""时间工具函数模块。"""

from __future__ import annotations

import zoneinfo
from datetime import datetime

# 中文星期名称，索引与 datetime.weekday() 对齐（0=周一）
_WEEKDAY_NAMES_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def is_quiet_time(quiet_hours_str: str, tz: zoneinfo.ZoneInfo | None) -> bool:
    """检查当前时间是否处于免打扰时段。"""
    try:
        # 解析开始与结束小时
        start_str, end_str = quiet_hours_str.split("-")
        start_hour, end_hour = int(start_str), int(end_str)
        # 若未提供时区则使用系统本地时间
        now = datetime.now(tz) if tz else datetime.now()
        # 处理跨天区间
        if start_hour <= end_hour:
            # 同日区间，例如 1-7
            return start_hour <= now.hour < end_hour
        # 跨日区间，例如 23-6
        return now.hour >= start_hour or now.hour < end_hour
    except (ValueError, TypeError):
        # 配置非法时按“非免打扰”处理，避免误阻断主动消息
        return False


def format_current_time(tz: zoneinfo.ZoneInfo | None = None) -> str:
    """返回带中文星期的时间字符串，例如 2026年05月12日 周二 09:53。

    星期信息直接由 datetime 计算得出，避免模型自行由日期反推星期几而算错。
    """
    now = datetime.now(tz) if tz else datetime.now()
    weekday = _WEEKDAY_NAMES_ZH[now.weekday()]
    return f"{now.strftime('%Y年%m月%d日')} {weekday} {now.strftime('%H:%M')}"
