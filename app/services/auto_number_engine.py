"""Auto-number field helpers.

Records store the immutable numeric sequence. Presentation (prefix/padding) is
field configuration, so changing display format never rewrites historical rows.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple


class AutoNumberValidationError(ValueError):
    pass


def is_auto_number_field(field: Dict[str, Any]) -> bool:
    return str(field.get("type") or "") == "autoNumber"


def normalize_auto_number_property(
    property_value: Any,
    *,
    preserve_next: int | None = None,
) -> Dict[str, Any]:
    prop = dict(property_value or {})
    prefix = prop.get("prefix", "")
    if prefix is None:
        prefix = ""
    if not isinstance(prefix, str):
        raise AutoNumberValidationError("Auto-number prefix must be text")
    if len(prefix) > 64:
        raise AutoNumberValidationError("Auto-number prefix cannot exceed 64 characters")

    try:
        digits = int(prop.get("digits", 3))
    except (TypeError, ValueError) as exc:
        raise AutoNumberValidationError("Auto-number digits must be an integer") from exc
    if digits < 1 or digits > 12:
        raise AutoNumberValidationError("Auto-number digits must be between 1 and 12")

    try:
        start = int(prop.get("start", 1))
    except (TypeError, ValueError) as exc:
        raise AutoNumberValidationError("Auto-number start must be an integer") from exc
    if start < 1 or start > 999_999_999_999:
        raise AutoNumberValidationError("Auto-number start must be between 1 and 999999999999")

    if preserve_next is None:
        raw_next = prop.get("nextNumber", start)
        try:
            next_number = int(raw_next)
        except (TypeError, ValueError):
            next_number = start
        next_number = max(start, next_number)
    else:
        next_number = max(start, int(preserve_next))

    prop.update(
        {
            "prefix": prefix,
            "digits": digits,
            "start": start,
            "nextNumber": next_number,
        }
    )
    return prop


def validate_auto_number_field(field: Dict[str, Any]) -> None:
    if not is_auto_number_field(field):
        return
    normalize_auto_number_property(field.get("property"))


def allocate_auto_number_values(
    property_value: Any,
    count: int,
) -> Tuple[list[int], Dict[str, Any]]:
    prop = normalize_auto_number_property(property_value)
    amount = max(0, int(count))
    first = int(prop["nextNumber"])
    values = list(range(first, first + amount))
    prop["nextNumber"] = first + amount
    return values, prop


def format_auto_number(field: Dict[str, Any], value: Any) -> str:
    if value is None or value == "":
        return ""
    prop = normalize_auto_number_property(field.get("property"))
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{prop['prefix']}{number:0{prop['digits']}d}"
