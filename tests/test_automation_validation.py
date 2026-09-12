from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.smart_table import TableField
from app.services.automation.scheduler import _due_value_utc
from app.services.automation.validation import (
    AutomationValidationError,
    evaluate_conditions,
    normalize_conditions,
    normalize_trigger,
)


def _fields():
    points = TableField(
        id="points",
        table_id="table-validation",
        name="Points",
        type="number",
        order_index=0,
    )
    due = TableField(
        id="due",
        table_id="table-validation",
        name="Due",
        type="date",
        order_index=1,
    )
    return {"points": points, "due": due}


def test_numeric_condition_values_are_validated_and_normalized():
    fields = _fields()
    normalized = normalize_conditions(
        {"fieldId": "points", "operator": "gte", "value": "3.5"},
        fields,
    )
    assert normalized["value"] == 3.5

    membership = normalize_conditions(
        {"fieldId": "points", "operator": "in", "value": ["1", 2, 3.5]},
        fields,
    )
    assert membership["value"] == [1, 2, 3.5]

    with pytest.raises(AutomationValidationError, match="must be numeric"):
        normalize_conditions(
            {"fieldId": "points", "operator": "gt", "value": "three"},
            fields,
        )


def test_date_condition_and_transition_values_fail_closed_when_malformed():
    fields = _fields()
    with pytest.raises(AutomationValidationError, match="ISO date/datetime"):
        normalize_conditions(
            {"fieldId": "due", "operator": "lt", "value": "tomorrow-ish"},
            fields,
        )

    with pytest.raises(AutomationValidationError, match="ISO date/datetime"):
        normalize_trigger(
            {
                "type": "record.updated",
                "fieldIds": ["due"],
                "from": "not-a-date",
                "to": "2026-09-08T09:00:00+09:00",
            },
            fields,
        )


def test_transition_values_are_normalized_and_allow_empty_edges():
    numeric = normalize_trigger(
        {
            "type": "record.updated",
            "fieldIds": ["points"],
            "from": "3",
            "to": "4.5",
        },
        _fields(),
    )
    assert numeric["from"] == 3
    assert numeric["to"] == 4.5

    empty_to_date = normalize_trigger(
        {
            "type": "record.updated",
            "fieldIds": ["due"],
            "from": None,
            "to": "2026-09-08T09:00:00+09:00",
        },
        _fields(),
    )
    assert empty_to_date["from"] is None


def test_date_conditions_compare_instants_without_naive_aware_errors():
    fields = _fields()
    conditions = normalize_conditions(
        {
            "fieldId": "due",
            "operator": "equals",
            "value": "2026-09-08T09:00:00+09:00",
        },
        fields,
    )
    assert evaluate_conditions(
        conditions,
        {"due": "2026-09-08T00:00:00Z"},
        fields,
    ) is True

    ordered = normalize_conditions(
        {
            "fieldId": "due",
            "operator": "lt",
            "value": "2026-09-08T01:00:00+00:00",
        },
        fields,
    )
    # Naive values are made comparable instead of raising TypeError.
    assert evaluate_conditions(
        ordered,
        {"due": "2026-09-08T00:30:00"},
        fields,
    ) is True


def test_naive_due_value_uses_rule_timezone_before_utc_conversion():
    # 09:00 in Tokyo is midnight UTC. Treating this legacy/local value as UTC
    # would shift the automation by nine hours.
    assert _due_value_utc("2026-09-08T09:00:00", "Asia/Tokyo") == datetime(
        2026,
        9,
        8,
        0,
        0,
        tzinfo=timezone.utc,
    )
    assert _due_value_utc("not-a-date", "Asia/Tokyo") is None
