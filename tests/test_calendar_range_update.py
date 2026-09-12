from __future__ import annotations

import pytest

from app.services.calendar_range import (
    CalendarRangeError,
    validate_calendar_range_update,
)


FIELDS = [
    {"id": "title", "name": "任务", "type": "text"},
    {"id": "start", "name": "开始时间", "type": "date"},
    {"id": "end", "name": "结束时间", "type": "date"},
]


def validate(start_value, end_value, **overrides):
    return validate_calendar_range_update(
        FIELDS,
        start_field_id=overrides.get("start_field_id", "start"),
        end_field_id=overrides.get("end_field_id", "end"),
        start_value=start_value,
        end_value=end_value,
    )


def test_valid_range_accepts_javascript_millisecond_timestamps():
    # 2026-08-04 -> 2026-08-08 UTC
    validate(1785801600000, 1786147200000)


def test_valid_range_accepts_iso_date_strings():
    validate("2026-08-04", "2026-08-08")


def test_same_day_range_is_valid():
    validate("2026-08-04T09:00:00", "2026-08-04T18:00:00")


def test_start_after_end_is_rejected():
    with pytest.raises(CalendarRangeError, match="开始时间不能晚于结束时间"):
        validate("2026-08-09", "2026-08-08")


def test_clearing_both_values_is_valid_for_unscheduled_record():
    validate(None, None)


def test_end_without_start_is_rejected():
    with pytest.raises(
        CalendarRangeError,
        match="开始时间为空时结束时间也必须为空",
    ):
        validate(None, "2026-08-08")


def test_start_and_end_fields_must_be_distinct():
    with pytest.raises(
        CalendarRangeError,
        match="开始时间和结束时间不能使用同一个字段",
    ):
        validate(
            "2026-08-04",
            "2026-08-08",
            end_field_id="start",
        )


def test_start_field_must_exist_and_be_date():
    with pytest.raises(CalendarRangeError, match="开始时间字段不存在"):
        validate(
            "2026-08-04",
            "2026-08-08",
            start_field_id="missing",
        )

    with pytest.raises(CalendarRangeError, match="开始时间字段必须是日期字段"):
        validate(
            "2026-08-04",
            "2026-08-08",
            start_field_id="title",
        )


def test_end_field_must_exist_and_be_date():
    with pytest.raises(CalendarRangeError, match="结束时间字段不存在"):
        validate(
            "2026-08-04",
            "2026-08-08",
            end_field_id="missing",
        )

    with pytest.raises(CalendarRangeError, match="结束时间字段必须是日期字段"):
        validate(
            "2026-08-04",
            "2026-08-08",
            end_field_id="title",
        )


def test_end_value_requires_end_field():
    with pytest.raises(
        CalendarRangeError,
        match="未配置结束时间字段时不能写入结束时间",
    ):
        validate_calendar_range_update(
            FIELDS,
            start_field_id="start",
            end_field_id=None,
            start_value="2026-08-04",
            end_value="2026-08-08",
        )


def test_invalid_date_value_is_rejected():
    with pytest.raises(CalendarRangeError, match="开始时间不是有效日期"):
        validate("not-a-date", "2026-08-08")
