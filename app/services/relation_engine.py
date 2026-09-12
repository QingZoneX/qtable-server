"""Validation and normalization helpers for SmartTable link-to-record fields.

Relation fields keep only target record IDs in ``TableRecord.data``. Display
labels are resolved from the target table at read/query time so renaming a
linked record never requires rewriting source records.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

RELATION_FIELD_TYPE = "relation"
MAX_LINKED_RECORDS = 200
MAX_RECORD_ID_LENGTH = 256


class RelationValidationError(ValueError):
    """Raised when a relation field configuration or value is invalid."""


def is_relation_field(field: Mapping[str, Any]) -> bool:
    return str(field.get("type") or "").lower() == RELATION_FIELD_TYPE


def get_relation_property(field: Mapping[str, Any]) -> Mapping[str, Any]:
    prop = field.get("property")
    return prop if isinstance(prop, Mapping) else {}


def get_target_table_id(field: Mapping[str, Any]) -> str:
    value = get_relation_property(field).get("targetTableId")
    return value.strip() if isinstance(value, str) else ""


def get_display_field_id(field: Mapping[str, Any]) -> str | None:
    value = get_relation_property(field).get("displayFieldId")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def relation_allows_multiple(field: Mapping[str, Any]) -> bool:
    return bool(get_relation_property(field).get("multiple", True))


def validate_relation_field_shape(field: Mapping[str, Any]) -> None:
    """Validate the persistence-independent shape of a relation field."""
    if not is_relation_field(field):
        return
    target_table_id = get_target_table_id(field)
    if not target_table_id:
        raise RelationValidationError("Relation field requires property.targetTableId")
    if len(target_table_id) > MAX_RECORD_ID_LENGTH:
        raise RelationValidationError("Relation target table id is too long")

    prop = get_relation_property(field)
    multiple = prop.get("multiple", True)
    if not isinstance(multiple, bool):
        raise RelationValidationError("Relation property.multiple must be a boolean")

    display_field_id = prop.get("displayFieldId")
    if display_field_id is not None and not isinstance(display_field_id, str):
        raise RelationValidationError("Relation property.displayFieldId must be a string")


def validate_relation_schema(fields: Sequence[Mapping[str, Any]]) -> None:
    for field in fields:
        validate_relation_field_shape(field)


def _normalize_record_id(value: Any) -> str:
    if not isinstance(value, str):
        raise RelationValidationError("Linked record id must be a string")
    record_id = value.strip()
    if not record_id:
        raise RelationValidationError("Linked record id cannot be empty")
    if len(record_id) > MAX_RECORD_ID_LENGTH:
        raise RelationValidationError("Linked record id is too long")
    return record_id


def normalize_relation_value(field: Mapping[str, Any], value: Any) -> str | List[str] | None:
    """Normalize a relation value into the canonical stored representation.

    Single-link field: ``None | str``
    Multi-link field: ``list[str]`` (deduplicated, order preserving)
    """
    if not is_relation_field(field):
        return value

    multiple = relation_allows_multiple(field)
    if value is None or value == "":
        return [] if multiple else None

    raw_values: Iterable[Any]
    if isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raw_values = [value]

    normalized: List[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        if raw is None or raw == "":
            continue
        record_id = _normalize_record_id(raw)
        if record_id in seen:
            continue
        seen.add(record_id)
        normalized.append(record_id)
        if len(normalized) > MAX_LINKED_RECORDS:
            raise RelationValidationError(
                f"Relation field cannot link more than {MAX_LINKED_RECORDS} records"
            )

    if multiple:
        return normalized
    if len(normalized) > 1:
        raise RelationValidationError("Single relation field accepts only one linked record")
    return normalized[0] if normalized else None


def relation_record_ids(field: Mapping[str, Any], value: Any) -> List[str]:
    normalized = normalize_relation_value(field, value)
    if normalized is None:
        return []
    if isinstance(normalized, list):
        return normalized
    return [normalized]


def remove_linked_record(
    field: Mapping[str, Any], value: Any, deleted_record_id: str
) -> Tuple[Any, bool]:
    """Remove a deleted target record ID from one stored relation value."""
    if not is_relation_field(field):
        return value, False
    normalized = normalize_relation_value(field, value)
    if isinstance(normalized, list):
        next_value = [item for item in normalized if item != deleted_record_id]
        return next_value, next_value != normalized
    if normalized == deleted_record_id:
        return None, True
    return normalized, False


def relation_fields_targeting(
    fields: Sequence[Mapping[str, Any]], target_table_id: str
) -> List[Dict[str, Any]]:
    return [
        dict(field)
        for field in fields
        if is_relation_field(field) and get_target_table_id(field) == target_table_id
    ]
