"""Validation helpers for atomic calendar range updates."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class CalendarRangeError(ValueError):
    """Raised when a calendar range update is invalid."""


def _to_datetime(value: Any, label: str) -> Optional[datetime]:
    if value is None or value == "":
        return None

    parsed: Optional[datetime] = None

    if isinstance(value, (int, float)):
        # Calendar values in QTable are commonly JavaScript timestamps (ms).
        seconds = float(value)
        if abs(seconds) > 100_000_000_000:
            seconds /= 1000.0
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise CalendarRangeError(f"{label}不是有效日期") from exc

    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None

        # Numeric strings are accepted for compatibility with imported values.
        try:
            numeric = float(cleaned)
        except ValueError:
            numeric = None
        if numeric is not None:
            return _to_datetime(numeric, label)

        normalized = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            # Date-only values are already supported by fromisoformat; keep the
            # user-facing error concise for other unsupported strings.
            raise CalendarRangeError(f"{label}不是有效日期")

    elif isinstance(value, datetime):
        parsed = value

    else:
        raise CalendarRangeError(f"{label}不是有效日期")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_calendar_range_update(
    fields: List[Dict[str, Any]],
    *,
    start_field_id: str,
    end_field_id: Optional[str],
    start_value: Any,
    end_value: Any,
) -> None:
    """Validate field identities/types and range ordering before a write."""

    fields_by_id = {
        str(field.get("id")): field
        for field in fields
        if field.get("id") is not None
    }

    start_field = fields_by_id.get(start_field_id)
    if not start_field:
        raise CalendarRangeError("开始时间字段不存在")
    if str(start_field.get("type")) != "date":
        raise CalendarRangeError("开始时间字段必须是日期字段")

    end_field = None
    if end_field_id:
        if end_field_id == start_field_id:
            raise CalendarRangeError("开始时间和结束时间不能使用同一个字段")
        end_field = fields_by_id.get(end_field_id)
        if not end_field:
            raise CalendarRangeError("结束时间字段不存在")
        if str(end_field.get("type")) != "date":
            raise CalendarRangeError("结束时间字段必须是日期字段")
    elif end_value not in (None, ""):
        raise CalendarRangeError("未配置结束时间字段时不能写入结束时间")

    start_dt = _to_datetime(start_value, "开始时间")
    end_dt = _to_datetime(end_value, "结束时间") if end_field else None

    # Unscheduled records are represented by clearing both values.
    if start_dt is None:
        if end_dt is not None:
            raise CalendarRangeError("开始时间为空时结束时间也必须为空")
        return

    if end_dt is not None and start_dt > end_dt:
        raise CalendarRangeError("开始时间不能晚于结束时间")
