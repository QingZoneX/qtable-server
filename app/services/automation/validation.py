from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField


TRIGGER_TYPES = {"record.created", "record.updated", "scheduled", "due_date", "manual"}
CONDITION_GROUP_OPS = {"and", "or"}
BASE_CONDITION_OPERATORS = {
    "equals",
    "not_equals",
    "in",
    "not_in",
    "empty",
    "not_empty",
}
TEXT_CONDITION_OPERATORS = BASE_CONDITION_OPERATORS | {"contains", "not_contains"}
ORDER_CONDITION_OPERATORS = BASE_CONDITION_OPERATORS | {"gt", "gte", "lt", "lte"}
NUMERIC_FIELD_TYPES = {"number", "progress", "rating", "autoNumber", "auto_number"}
DATE_FIELD_TYPES = {"date", "datetime", "createdTime", "modifiedTime"}
MEMBER_FIELD_TYPES = {"member"}
NON_WRITABLE_FIELD_TYPES = {
    "formula",
    "autoNumber",
    "auto_number",
    "createdTime",
    "modifiedTime",
    "createdBy",
    "modifiedBy",
}
ACTION_TYPES = {"update_record", "create_record", "notify"}
NOTIFICATION_TYPES = {
    "automation",
    "due_3d",
    "due_24h",
    "task_assigned",
    "ai_action_required",
    "automation_failed",
}
SENSITIVE_KEY_PARTS = ("secret", "token", "password", "credential", "api_key", "apikey")
MAX_CONDITION_DEPTH = 8
MAX_CONDITION_ITEMS = 64
MAX_ACTIONS = 32
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 60 * 24 * 30
MAX_DUE_OFFSET_MINUTES = 60 * 24 * 365
MAX_RETRIES = 10


class AutomationValidationError(ValueError):
    pass


def normalize_timezone(value: Optional[str]) -> str:
    zone = str(value or "UTC").strip() or "UTC"
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError as exc:
        raise AutomationValidationError(f"Unknown timezone: {zone}") from exc
    return zone


def normalize_max_retries(value: Any) -> int:
    try:
        retries = int(value)
    except (TypeError, ValueError) as exc:
        raise AutomationValidationError("maxRetries must be an integer") from exc
    if retries < 0 or retries > MAX_RETRIES:
        raise AutomationValidationError(f"maxRetries must be between 0 and {MAX_RETRIES}")
    return retries


def redact_sensitive(value: Any) -> Any:
    """Return a log-safe deep copy without secret-shaped values."""
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                output[key_text] = "[REDACTED]"
            else:
                output[key_text] = redact_sensitive(item)
        return output
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def _field_map(fields: Iterable[TableField]) -> Dict[str, TableField]:
    return {str(field.id): field for field in fields}


def _require_field(fields: Dict[str, TableField], field_id: Any) -> TableField:
    key = str(field_id or "").strip()
    field = fields.get(key)
    if not field:
        raise AutomationValidationError(f"Unknown field: {key or '<empty>'}")
    return field


def _normalize_field_ids(value: Any, fields: Dict[str, TableField]) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AutomationValidationError("fieldIds must be a list")
    result: List[str] = []
    for item in value:
        field = _require_field(fields, item)
        if field.id not in result:
            result.append(str(field.id))
    return result


def parse_datetime_value(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _normalize_typed_scalar(
    field: TableField,
    value: Any,
    *,
    label: str,
    allow_none: bool = False,
) -> Any:
    if value is None and allow_none:
        return None
    field_type = str(field.type)
    if field_type in NUMERIC_FIELD_TYPES:
        if isinstance(value, bool) or value is None:
            raise AutomationValidationError(
                f"{label} for field '{field.name}' must be numeric"
            )
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise AutomationValidationError(
                f"{label} for field '{field.name}' must be numeric"
            ) from exc
        return int(numeric) if numeric.is_integer() else numeric
    if field_type in DATE_FIELD_TYPES:
        if parse_datetime_value(value) is None:
            raise AutomationValidationError(
                f"{label} for field '{field.name}' must be an ISO date/datetime"
            )
    return value


def _normalize_typed_condition_value(
    field: TableField,
    operator: str,
    value: Any,
) -> Any:
    values = value if operator in {"in", "not_in"} else [value]
    normalized = [
        _normalize_typed_scalar(field, item, label=f"Condition value ({operator})")
        for item in values
    ]
    return normalized if operator in {"in", "not_in"} else normalized[0]


def normalize_trigger(trigger: Any, fields: Dict[str, TableField]) -> Dict[str, Any]:
    if not isinstance(trigger, Mapping):
        raise AutomationValidationError("trigger must be an object")
    trigger_type = str(trigger.get("type") or "").strip()
    if trigger_type not in TRIGGER_TYPES:
        raise AutomationValidationError(f"Unsupported trigger type: {trigger_type}")

    normalized: Dict[str, Any] = {"type": trigger_type}
    if trigger_type == "record.updated":
        field_ids = _normalize_field_ids(trigger.get("fieldIds"), fields)
        if trigger.get("fieldId") is not None:
            field = _require_field(fields, trigger.get("fieldId"))
            if str(field.id) not in field_ids:
                field_ids.append(str(field.id))
        if field_ids:
            normalized["fieldIds"] = field_ids
        if ("from" in trigger or "to" in trigger) and len(field_ids) != 1:
            raise AutomationValidationError(
                "record.updated from/to matching requires exactly one field"
            )
        transition_field = fields[field_ids[0]] if len(field_ids) == 1 else None
        if "from" in trigger:
            normalized["from"] = (
                _normalize_typed_scalar(
                    transition_field,
                    trigger.get("from"),
                    label="Trigger from value",
                    allow_none=True,
                )
                if transition_field is not None
                else trigger.get("from")
            )
        if "to" in trigger:
            normalized["to"] = (
                _normalize_typed_scalar(
                    transition_field,
                    trigger.get("to"),
                    label="Trigger to value",
                    allow_none=True,
                )
                if transition_field is not None
                else trigger.get("to")
            )

    elif trigger_type == "scheduled":
        try:
            interval = int(trigger.get("intervalMinutes"))
        except (TypeError, ValueError) as exc:
            raise AutomationValidationError("scheduled trigger requires intervalMinutes") from exc
        if interval < MIN_INTERVAL_MINUTES or interval > MAX_INTERVAL_MINUTES:
            raise AutomationValidationError(
                f"intervalMinutes must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES}"
            )
        normalized["intervalMinutes"] = interval

    elif trigger_type == "due_date":
        field = _require_field(fields, trigger.get("fieldId"))
        if str(field.type) not in DATE_FIELD_TYPES:
            raise AutomationValidationError("due_date trigger requires a date field")
        try:
            offset = int(trigger.get("offsetMinutes", 0))
            scan_interval = int(trigger.get("scanIntervalMinutes", 5))
        except (TypeError, ValueError) as exc:
            raise AutomationValidationError("due_date offsets must be integers") from exc
        if offset < 0 or offset > MAX_DUE_OFFSET_MINUTES:
            raise AutomationValidationError(
                f"offsetMinutes must be between 0 and {MAX_DUE_OFFSET_MINUTES}"
            )
        if scan_interval < MIN_INTERVAL_MINUTES or scan_interval > 1440:
            raise AutomationValidationError("scanIntervalMinutes must be between 1 and 1440")
        normalized.update(
            {
                "fieldId": str(field.id),
                "offsetMinutes": offset,
                "scanIntervalMinutes": scan_interval,
            }
        )

    return normalized


def _allowed_operators(field: TableField) -> set[str]:
    field_type = str(field.type)
    if field_type in NUMERIC_FIELD_TYPES or field_type in DATE_FIELD_TYPES:
        return ORDER_CONDITION_OPERATORS
    return TEXT_CONDITION_OPERATORS


def normalize_conditions(
    conditions: Any,
    fields: Dict[str, TableField],
    *,
    depth: int = 0,
    item_counter: Optional[List[int]] = None,
) -> Dict[str, Any]:
    if conditions in (None, {}, []):
        return {"op": "and", "items": []}
    if depth > MAX_CONDITION_DEPTH:
        raise AutomationValidationError("Condition nesting is too deep")
    if not isinstance(conditions, Mapping):
        raise AutomationValidationError("conditions must be an object")

    counter = item_counter if item_counter is not None else [0]
    if "items" in conditions or str(conditions.get("op") or "").lower() in CONDITION_GROUP_OPS:
        op = str(conditions.get("op") or "and").strip().lower()
        if op not in CONDITION_GROUP_OPS:
            raise AutomationValidationError("Condition group op must be 'and' or 'or'")
        items = conditions.get("items") or []
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise AutomationValidationError("Condition group items must be a list")
        normalized_items: List[Dict[str, Any]] = []
        for item in items:
            counter[0] += 1
            if counter[0] > MAX_CONDITION_ITEMS:
                raise AutomationValidationError("Too many conditions")
            normalized_items.append(
                normalize_conditions(item, fields, depth=depth + 1, item_counter=counter)
            )
        return {"op": op, "items": normalized_items}

    field = _require_field(fields, conditions.get("fieldId"))
    operator = str(conditions.get("operator") or "").strip().lower()
    if operator not in _allowed_operators(field):
        raise AutomationValidationError(
            f"Operator '{operator}' is not valid for field '{field.name}' ({field.type})"
        )
    normalized = {"fieldId": str(field.id), "operator": operator}
    if operator not in {"empty", "not_empty"}:
        if "value" not in conditions:
            raise AutomationValidationError(f"Operator '{operator}' requires value")
        value = conditions.get("value")
        if operator in {"in", "not_in"} and not isinstance(value, list):
            raise AutomationValidationError(f"Operator '{operator}' requires a list value")
        normalized["value"] = _normalize_typed_condition_value(field, operator, value)
    return normalized


def _normalize_write_fields(
    value: Any,
    fields: Dict[str, TableField],
    *,
    action_name: str,
) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise AutomationValidationError(f"{action_name} requires a non-empty fields object")
    normalized: Dict[str, Any] = {}
    for raw_field_id, item in value.items():
        field = _require_field(fields, raw_field_id)
        if str(field.type) in NON_WRITABLE_FIELD_TYPES:
            raise AutomationValidationError(
                f"Field '{field.name}' ({field.type}) cannot be written by automation"
            )
        normalized[str(field.id)] = item
    return normalized


def normalize_actions(actions: Any, fields: Dict[str, TableField]) -> List[Dict[str, Any]]:
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise AutomationValidationError("actions must be a list")
    if not actions:
        raise AutomationValidationError("At least one action is required")
    if len(actions) > MAX_ACTIONS:
        raise AutomationValidationError(f"No more than {MAX_ACTIONS} actions are allowed")

    result: List[Dict[str, Any]] = []
    for index, raw in enumerate(actions):
        if not isinstance(raw, Mapping):
            raise AutomationValidationError(f"Action {index + 1} must be an object")
        action_type = str(raw.get("type") or "").strip()
        if action_type not in ACTION_TYPES:
            raise AutomationValidationError(f"Unsupported action type: {action_type}")
        normalized: Dict[str, Any] = {"type": action_type}

        if action_type in {"update_record", "create_record"}:
            normalized["fields"] = _normalize_write_fields(
                raw.get("fields"), fields, action_name=action_type
            )
        elif action_type == "notify":
            recipients: List[int] = []
            raw_recipients = raw.get("recipientUserIds") or []
            if not isinstance(raw_recipients, Sequence) or isinstance(raw_recipients, (str, bytes)):
                raise AutomationValidationError("recipientUserIds must be a list")
            for recipient in raw_recipients:
                try:
                    value = int(recipient)
                except (TypeError, ValueError) as exc:
                    raise AutomationValidationError("recipientUserIds must contain integers") from exc
                if value > 0 and value not in recipients:
                    recipients.append(value)
            if recipients:
                normalized["recipientUserIds"] = recipients

            recipient_field_id = raw.get("recipientFieldId")
            if recipient_field_id is not None:
                field = _require_field(fields, recipient_field_id)
                if str(field.type) not in MEMBER_FIELD_TYPES:
                    raise AutomationValidationError("recipientFieldId must reference a member field")
                normalized["recipientFieldId"] = str(field.id)

            if not recipients and "recipientFieldId" not in normalized:
                raise AutomationValidationError(
                    "notify requires recipientUserIds or recipientFieldId"
                )
            notification_type = str(raw.get("notificationType") or "automation").strip()
            if notification_type not in NOTIFICATION_TYPES:
                raise AutomationValidationError(
                    f"Unsupported notificationType: {notification_type}"
                )
            message = str(raw.get("message") or "自动化规则已触发").strip()
            if not message:
                raise AutomationValidationError("notify message cannot be empty")
            if len(message) > 500:
                raise AutomationValidationError("notify message cannot exceed 500 characters")
            normalized["notificationType"] = notification_type
            normalized["message"] = message

        result.append(normalized)
    return result


def required_permission_for_actions(actions: Sequence[Mapping[str, Any]]) -> str:
    return (
        "update"
        if any(action.get("type") in {"update_record", "create_record"} for action in actions)
        else "read"
    )


async def validate_automation_definition(
    db: AsyncSession,
    *,
    table_id: str,
    trigger: Any,
    conditions: Any,
    actions: Any,
    timezone: Optional[str] = "UTC",
    max_retries: Any = 3,
) -> Dict[str, Any]:
    field_result = await db.execute(
        select(TableField)
        .where(TableField.table_id == str(table_id))
        .order_by(TableField.order_index.asc())
    )
    fields = list(field_result.scalars().all())
    if not fields:
        raise AutomationValidationError("Table has no fields")
    by_id = _field_map(fields)
    return {
        "trigger": normalize_trigger(trigger, by_id),
        "conditions": normalize_conditions(conditions, by_id),
        "actions": normalize_actions(actions, by_id),
        "timezone": normalize_timezone(timezone),
        "maxRetries": normalize_max_retries(max_retries),
    }


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _coerce_order_value(value: Any, field_type: str) -> Any:
    if field_type in NUMERIC_FIELD_TYPES:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field_type in DATE_FIELD_TYPES:
        parsed = parse_datetime_value(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return value


def evaluate_conditions(
    conditions: Mapping[str, Any],
    record: Mapping[str, Any],
    fields: Mapping[str, TableField],
) -> bool:
    if "items" in conditions:
        op = str(conditions.get("op") or "and")
        results = [evaluate_conditions(item, record, fields) for item in conditions.get("items") or []]
        return all(results) if op == "and" else any(results)

    field_id = str(conditions.get("fieldId") or "")
    field = fields.get(field_id)
    if field is None:
        return False
    field_type = str(field.type)
    operator = str(conditions.get("operator") or "")
    actual = record.get(field_id)
    expected = conditions.get("value")

    if operator == "empty":
        return _is_empty(actual)
    if operator == "not_empty":
        return not _is_empty(actual)
    if operator in {"equals", "not_equals"}:
        if field_type in NUMERIC_FIELD_TYPES or field_type in DATE_FIELD_TYPES:
            actual_value = _coerce_order_value(actual, field_type)
            expected_value = _coerce_order_value(expected, field_type)
            equals = actual_value is not None and actual_value == expected_value
        else:
            equals = actual == expected
        return equals if operator == "equals" else not equals
    if operator in {"in", "not_in"}:
        if field_type in NUMERIC_FIELD_TYPES or field_type in DATE_FIELD_TYPES:
            actual_value = _coerce_order_value(actual, field_type)
            expected_values = [
                _coerce_order_value(item, field_type) for item in (expected or [])
            ]
            contains = actual_value is not None and actual_value in expected_values
        else:
            contains = actual in (expected or [])
        return contains if operator == "in" else not contains
    if operator in {"contains", "not_contains"}:
        if isinstance(actual, list):
            contains = expected in actual
        else:
            contains = str(expected or "") in str(actual or "")
        return contains if operator == "contains" else not contains

    actual_value = _coerce_order_value(actual, field_type)
    expected_value = _coerce_order_value(expected, field_type)
    if actual_value is None or expected_value is None:
        return False
    if operator == "gt":
        return actual_value > expected_value
    if operator == "gte":
        return actual_value >= expected_value
    if operator == "lt":
        return actual_value < expected_value
    if operator == "lte":
        return actual_value <= expected_value
    return False
