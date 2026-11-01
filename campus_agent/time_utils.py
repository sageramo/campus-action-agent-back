"""Shared, timezone-aware clocks; invalid inputs never become 'now'."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MIDNIGHT_24 = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T]24:00(?::00)?(Z|[+-]\d{2}:\d{2})?$")


def get_zone(timezone: str = DEFAULT_TIMEZONE) -> ZoneInfo:
    if timezone != DEFAULT_TIMEZONE:
        raise ValueError("timezone 当前仅支持 Asia/Shanghai")
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("未安装 Asia/Shanghai 时区数据") from exc


def parse_dt(value: str | None, timezone: str = DEFAULT_TIMEZONE) -> datetime | None:
    zone = get_zone(timezone)
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    offset = re.search(r"[+-](\d{2}):(\d{2})$", value)
    if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):
        return None
    midnight = MIDNIGHT_24.fullmatch(value)
    try:
        if midnight:
            next_date = date.fromisoformat(midnight[1]) + timedelta(days=1)
            value = f"{next_date.isoformat()}T00:00{midnight[2] or ''}"
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?", value):
            return None
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=zone)
        return result.astimezone(zone)
    except (ValueError, OverflowError):
        return None


def require_datetime(value: object, field: str, timezone: str = DEFAULT_TIMEZONE) -> datetime:
    if not isinstance(value, str) or DATE_ONLY.fullmatch(value.strip()):
        raise ValueError(f"{field} 必须包含日期和具体时刻，不能擅自把仅有日期当成零点或23:59")
    result = parse_dt(value, timezone)
    if result is None:
        raise ValueError(f"{field} 不是有效日期时间")
    return result


def reference_day(value: str | None, timezone: str = DEFAULT_TIMEZONE) -> date | None:
    get_zone(timezone)
    if value is None:
        return None
    if not isinstance(value, str) or not DATE_ONLY.fullmatch(value):
        raise ValueError("reference_date 必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("reference_date 不是有效日期") from exc
