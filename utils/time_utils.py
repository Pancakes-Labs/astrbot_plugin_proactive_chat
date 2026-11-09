"""时间工具函数模块。"""

from __future__ import annotations

import re
import zoneinfo
from datetime import datetime, timezone
from typing import Any

# 中文星期名称，索引与 datetime.weekday() 对齐（0=周一）
_WEEKDAY_NAMES_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 流水时间标签的两种形态：与当前同一天只显示时分，跨天才补月日，
# 避免每行都拖一串完整日期把上下文撑长。
_CLOCK_SAME_DAY_FORMAT = "%H:%M"
_CLOCK_OTHER_DAY_FORMAT = "%m-%d %H:%M"


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


def coerce_aware_datetime(
    value: Any,
    tz: zoneinfo.ZoneInfo | None = None,
) -> datetime | None:
    """把数据库里的时间字段归一化为带时区的 datetime。

    AstrBot 核心以 UTC 写入 created_at，但经 SQLModel 或 SQLite 读出后
    可能是 naive datetime，也可能是 "2026-11-08 06:31:22.477315" 这类
    字符串。缺失时区信息时一律按 UTC 解释，否则会整体偏移时区差。
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    # 未配置时区时统一用本机时区，与 format_current_time 的显示口径一致；
    # 若这里保留 UTC，流水时间标签会与提示词里的当前时间差一个时区。
    target_tz = tz or moment.astimezone().tzinfo
    try:
        return moment.astimezone(target_tz)
    except (ValueError, OverflowError):  # pragma: no cover - 防御性兜底
        return moment


def format_history_clock(
    value: Any,
    tz: zoneinfo.ZoneInfo | None = None,
    *,
    now: Any = None,
) -> str:
    """把单条流水时间渲染成紧凑标签，例如 09:53 或 11-07 22:40。

    与 now（缺省为当前时刻）同一天时只保留时分，跨天才补月日，
    保证每条记录都能看出“是什么时候说的”，又不至于让上下文暴长。
    """
    moment = coerce_aware_datetime(value, tz)
    if moment is None:
        return ""

    current = coerce_aware_datetime(now, tz) if now is not None else None
    if current is None:
        current = datetime.now(tz) if tz else datetime.now(timezone.utc)
        current = coerce_aware_datetime(current, tz)

    if current is not None and moment.date() == current.date():
        return moment.strftime(_CLOCK_SAME_DAY_FORMAT)
    return moment.strftime(_CLOCK_OTHER_DAY_FORMAT)


def format_relative_age(
    value: Any,
    tz: zoneinfo.ZoneInfo | None = None,
    *,
    now: Any = None,
) -> str:
    """把流水时间渲染成“刚刚 / 12 分钟前 / 3 小时前 / 2 天前”。

    模型无法自行比较时间差，显式给出“距今多久”才能判断一段聊天
    是正在进行还是早就结束了。
    """
    moment = coerce_aware_datetime(value, tz)
    if moment is None:
        return ""

    current = coerce_aware_datetime(now, tz) if now is not None else None
    if current is None:
        current = datetime.now(tz) if tz else datetime.now(timezone.utc)
        current = coerce_aware_datetime(current, tz)
    if current is None:
        return ""

    # 必须先转 UTC 再相减：两个 datetime 共用同一个 tzinfo 实例时，
    # Python 会忽略 tzinfo 直接做“墙上时间”减法，夏令时回拨那一小时
    # 会被算成间隔 0，显示成“刚刚”，误导模型判断话题新鲜度。
    seconds = (
        current.astimezone(timezone.utc) - moment.astimezone(timezone.utc)
    ).total_seconds()
    # 时钟轻微漂移或未来时间一律按“刚刚”处理，避免出现负数。
    if seconds < 60:
        return "刚刚"

    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟前"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"

    return f"{hours // 24} 天前"
