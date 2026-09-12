"""Server-side SmartTable record filtering, sorting and pagination."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from functools import cmp_to_key
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import Float, and_, case, cast, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.smart_table import TableRecord
from app.services.formula_engine import materialize_records


MAX_FILTERS = 50
MAX_SORTS = 20
MAX_QUERY_LIMIT = 1000
DEFAULT_QUERY_LIMIT = 200

_TEXT_SQL_TYPES = {"text", "url", "email", "phone"}
_NUMBER_SQL_TYPES = {"number", "progress", "rating"}
_SORT_TEXT_SQL_TYPES = _TEXT_SQL_TYPES | {"select"}
_SORT_NUMBER_SQL_TYPES = _NUMBER_SQL_TYPES | {"autoNumber"}


def _field_map(fields: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(field.get("id")): field
        for field in fields
        if field.get("id") is not None
    }


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, list) and len(value) == 0)


def _normalize_comparable(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("label", "name", "userName", "title", "id"):
            candidate = value.get(key)
            if candidate is not None and candidate != "":
                return str(candidate)
        return ""
    return str(value)


def _auto_number_display(field: Dict[str, Any], value: Any) -> str:
    if value is None or value == "":
        return ""
    prop = field.get("property") or {}
    prefix = prop.get("prefix") if isinstance(prop.get("prefix"), str) else ""
    try:
        digits = int(prop.get("digits", 3))
    except (TypeError, ValueError):
        digits = 3
    if digits < 1 or digits > 12:
        digits = 3
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(value)
    return f"{prefix}{str(math.trunc(number)).zfill(digits)}"


def _normalize_field_value(value: Any, field: Dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_normalize_field_value(item, field) for item in value]

    field_type = str(field.get("type") or "")
    if field_type in {"select", "multiSelect"}:
        primitive = _normalize_comparable(value)
        for option in field.get("options") or []:
            if not isinstance(option, dict):
                continue
            option_id = str(option.get("id") or "")
            label = str(option.get("label") or "")
            if primitive == option_id or primitive == label:
                return label
        return primitive

    if field_type == "member":
        def member_id(item: Any) -> str:
            if isinstance(item, dict):
                for key in ("id", "userId", "user_id", "value"):
                    candidate = item.get(key)
                    if candidate is not None and candidate != "":
                        return str(candidate)
                return ""
            return _normalize_comparable(item)

        if isinstance(value, list):
            return [member_id(item) for item in value if member_id(item)]
        return member_id(value)

    if field_type == "autoNumber":
        return _auto_number_display(field, value)

    return value


def _equals_filter_value(value: Any, expected: Any) -> bool:
    expected_text = _normalize_comparable(expected).casefold()
    if isinstance(value, list):
        return any(
            _normalize_comparable(item).casefold() == expected_text
            for item in value
        )
    return _normalize_comparable(value).casefold() == expected_text


def _number(value: Any) -> float:
    # Match JavaScript Number(...) semantics used by the current frontend
    # filter/sort helpers for the common persisted value shapes.
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, str) and value.strip() == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _date_timestamp(value: Any) -> float:
    if value is None or value == "":
        return float("nan")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) > 100_000_000_000:
            seconds /= 1000.0
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return float("nan")
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return float("nan")
        normalized = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return float("nan")
    else:
        return float("nan")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def check_filter(
    record: Dict[str, Any],
    condition: Dict[str, Any],
    fields: Sequence[Dict[str, Any]],
) -> bool:
    field = _field_map(fields).get(str(condition.get("fieldId") or ""))
    if not field:
        # Keep parity with the frontend: stale filters referencing deleted
        # fields are ignored instead of making the whole view fail.
        return True

    display_value = _normalize_field_value(record.get(str(field.get("id"))), field)
    expected = condition.get("value")
    operator = str(condition.get("operator") or "")

    if operator == "contains":
        needle = _normalize_comparable(expected).casefold()
        if isinstance(display_value, list):
            return any(
                needle in _normalize_comparable(item).casefold()
                for item in display_value
            )
        return needle in _normalize_comparable(display_value).casefold()
    if operator in {"equals", "is"}:
        return _equals_filter_value(display_value, expected)
    if operator == "is_not":
        return not _equals_filter_value(display_value, expected)
    if operator == "is_empty":
        return _is_empty(display_value)
    if operator == "is_not_empty":
        return not _is_empty(display_value)
    if operator == "gt":
        return _number(display_value) > _number(expected)
    if operator == "lt":
        return _number(display_value) < _number(expected)
    if operator == "gte":
        return _number(display_value) >= _number(expected)
    if operator == "lte":
        return _number(display_value) <= _number(expected)
    if operator == "before":
        return _date_timestamp(display_value) < _date_timestamp(expected)
    if operator == "after":
        return _date_timestamp(display_value) > _date_timestamp(expected)

    return True


def matches_filters(
    record: Dict[str, Any],
    filters: Sequence[Dict[str, Any]],
    fields: Sequence[Dict[str, Any]],
) -> bool:
    if not filters:
        return True

    matched = check_filter(record, filters[0], fields)
    for condition in filters[1:]:
        current = check_filter(record, condition, fields)
        matched = (
            matched or current
            if str(condition.get("logic") or "").lower() == "or"
            else matched and current
        )
    return matched


_NATURAL_PART = re.compile(r"(\d+(?:\.\d+)?)")


def _natural_key(value: Any) -> Tuple[Any, ...]:
    text = str(value).casefold()
    parts = _NATURAL_PART.split(text)
    result: List[Any] = []
    for part in parts:
        if not part:
            continue
        try:
            result.append((0, float(part)))
        except ValueError:
            result.append((1, part))
    return tuple(result)


def compare_smart_values(value_a: Any, value_b: Any, field: Optional[Dict[str, Any]]) -> int:
    if value_a == value_b:
        return 0
    if value_a is None or value_a == "":
        return 1
    if value_b is None or value_b == "":
        return -1

    field_type = str((field or {}).get("type") or "text")
    if field_type in {"number", "progress", "autoNumber"} or (
        field_type == "formula"
        and isinstance(value_a, (int, float))
        and not isinstance(value_a, bool)
        and isinstance(value_b, (int, float))
        and not isinstance(value_b, bool)
    ):
        left = _number(value_a)
        right = _number(value_b)
        if math.isfinite(left) and math.isfinite(right):
            return (left > right) - (left < right)

    if field_type == "date":
        left = _date_timestamp(value_a)
        right = _date_timestamp(value_b)
        if math.isfinite(left) and math.isfinite(right):
            return (left > right) - (left < right)

    if isinstance(value_a, bool) and isinstance(value_b, bool):
        return (value_a > value_b) - (value_a < value_b)

    left_key = _natural_key(value_a)
    right_key = _natural_key(value_b)
    return (left_key > right_key) - (left_key < right_key)


def sort_records(
    records: Sequence[Dict[str, Any]],
    sorts: Sequence[Dict[str, Any]],
    fields: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not sorts:
        return list(records)
    fields_by_id = _field_map(fields)

    def compare(left: Dict[str, Any], right: Dict[str, Any]) -> int:
        for sort in sorts:
            field_id = str(sort.get("fieldId") or "")
            field = fields_by_id.get(field_id)
            result = compare_smart_values(left.get(field_id), right.get(field_id), field)
            if str(sort.get("order") or "asc").lower() == "desc":
                result = -result
            if result != 0:
                return result
        return 0

    return sorted(records, key=cmp_to_key(compare))


def query_records_in_memory(
    fields: Sequence[Dict[str, Any]],
    records: Sequence[Dict[str, Any]],
    *,
    filters: Optional[Sequence[Dict[str, Any]]] = None,
    sorts: Optional[Sequence[Dict[str, Any]]] = None,
    offset: int = 0,
    limit: int = DEFAULT_QUERY_LIMIT,
) -> Dict[str, Any]:
    safe_filters = list(filters or [])
    safe_sorts = list(sorts or [])
    if len(safe_filters) > MAX_FILTERS:
        raise ValueError(f"Too many filters (max {MAX_FILTERS})")
    if len(safe_sorts) > MAX_SORTS:
        raise ValueError(f"Too many sorts (max {MAX_SORTS})")
    filtered = [
        record
        for record in records
        if matches_filters(record, safe_filters, fields)
    ]
    ordered = sort_records(filtered, safe_sorts, fields)
    normalized_offset = max(0, int(offset))
    normalized_limit = max(1, min(MAX_QUERY_LIMIT, int(limit)))
    page = ordered[normalized_offset : normalized_offset + normalized_limit]
    next_offset = normalized_offset + len(page)
    has_more = next_offset < len(ordered)
    return {
        "records": page,
        "totalCount": len(ordered),
        "offset": normalized_offset,
        "limit": normalized_limit,
        "hasMore": has_more,
        "nextOffset": next_offset if has_more else None,
    }


def _filters_support_exact_sql_paging(
    filters: Sequence[Dict[str, Any]],
    fields: Sequence[Dict[str, Any]],
) -> bool:
    """Return True only when SQL filtering is safe to page before Python parity checks.

    The existing query path deliberately re-checks SQL candidates in Python to
    preserve legacy value semantics. Large-table paging must not change those
    semantics, so this fast path is intentionally conservative.
    """
    if not filters:
        return True

    fields_by_id = _field_map(fields)
    for condition in filters:
        field = fields_by_id.get(str(condition.get("fieldId") or ""))
        if not field:
            return False
        field_type = str(field.get("type") or "")
        operator = str(condition.get("operator") or "")

        if operator in {"is_empty", "is_not_empty"} and field_type in (
            _TEXT_SQL_TYPES | _NUMBER_SQL_TYPES | {"select"}
        ):
            continue

        # Numeric comparison is exact for normalized numeric fields written by
        # QTable. Legacy/derived types stay on the compatibility path.
        if (
            field_type in _NUMBER_SQL_TYPES
            and operator in {"gt", "lt", "gte", "lte"}
        ):
            expected = _number(condition.get("value"))
            if math.isfinite(expected):
                continue

        return False
    return True


def _sorts_support_exact_sql_paging(
    sorts: Sequence[Dict[str, Any]],
    fields: Sequence[Dict[str, Any]],
) -> bool:
    """Natural text sorting needs Python; numeric ordering can page in SQL."""
    if not sorts:
        return True

    fields_by_id = _field_map(fields)
    for sort in sorts:
        field = fields_by_id.get(str(sort.get("fieldId") or ""))
        if not field:
            return False
        if str(field.get("type") or "") not in _SORT_NUMBER_SQL_TYPES:
            return False
    return True


def _text_expression(field_id: str):
    return TableRecord.data[field_id].as_string()


def _empty_expression(field_id: str):
    text_expr = _text_expression(field_id)
    return or_(text_expr.is_(None), text_expr == "")


def _select_expected(field: Dict[str, Any], expected: Any) -> Tuple[str, str]:
    text = _normalize_comparable(expected)
    folded = text.casefold()
    for option in field.get("options") or []:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id") or "")
        label = str(option.get("label") or "")
        if folded in {option_id.casefold(), label.casefold()}:
            return option_id, label
    return text, text


def compile_filter_condition(
    condition: Dict[str, Any],
    fields: Sequence[Dict[str, Any]],
) -> Optional[ColumnElement[bool]]:
    fields_by_id = _field_map(fields)
    field_id = str(condition.get("fieldId") or "")
    field = fields_by_id.get(field_id)
    if not field:
        return None

    field_type = str(field.get("type") or "")
    operator = str(condition.get("operator") or "")
    expected = condition.get("value")
    text_expr = _text_expression(field_id)
    lowered = func.lower(text_expr)

    if field_type in _TEXT_SQL_TYPES:
        expected_text = _normalize_comparable(expected).casefold()
        if operator == "contains":
            if expected_text == "":
                return true()
            return lowered.contains(expected_text, autoescape=True)
        if operator in {"equals", "is"}:
            if expected_text == "":
                return _empty_expression(field_id)
            return lowered == expected_text
        if operator == "is_empty":
            return _empty_expression(field_id)
        if operator == "is_not_empty":
            return ~_empty_expression(field_id)
        return None

    if field_type == "select":
        option_id, label = _select_expected(field, expected)
        alternatives = {option_id.casefold(), label.casefold()}
        if alternatives == {""}:
            equals_expr = _empty_expression(field_id)
        else:
            equals_expr = or_(*[lowered == item for item in alternatives])
        if operator in {"equals", "is"}:
            return equals_expr
        if operator == "is_not":
            return ~equals_expr
        if operator == "is_empty":
            return _empty_expression(field_id)
        if operator == "is_not_empty":
            return ~_empty_expression(field_id)
        return None

    if field_type in _NUMBER_SQL_TYPES:
        expected_text = _normalize_comparable(expected).casefold()
        if operator == "equals":
            # Frontend equality is textual even for number fields.
            if expected_text == "":
                return _empty_expression(field_id)
            return lowered == expected_text
        if operator == "is_empty":
            return _empty_expression(field_id)
        if operator == "is_not_empty":
            return ~_empty_expression(field_id)
        if operator in {"gt", "lt", "gte", "lte"}:
            numeric = _number(expected)
            if not math.isfinite(numeric):
                return false()
            number_expr = case(
                (_empty_expression(field_id), 0.0),
                else_=cast(text_expr, Float),
            )
            return {
                "gt": number_expr > numeric,
                "lt": number_expr < numeric,
                "gte": number_expr >= numeric,
                "lte": number_expr <= numeric,
            }[operator]
        return None

    # Auto-number filtering uses formatted prefix/zero-padding in the frontend;
    # date parsing can mix epoch and ISO values; arrays/objects and formula
    # values may be materialized. Keep those on the exact memory path.
    return None


def compile_filter_expression(
    filters: Sequence[Dict[str, Any]],
    fields: Sequence[Dict[str, Any]],
) -> Optional[ColumnElement[bool]]:
    if not filters:
        return None

    expressions: List[ColumnElement[bool]] = []
    for condition in filters:
        expression = compile_filter_condition(condition, fields)
        if expression is None:
            return None
        expressions.append(expression)

    combined = expressions[0]
    for index in range(1, len(expressions)):
        logic = str(filters[index].get("logic") or "").lower()
        combined = (
            or_(combined, expressions[index])
            if logic == "or"
            else and_(combined, expressions[index])
        )
    return combined


def compile_sort_expressions(
    sorts: Sequence[Dict[str, Any]],
    fields: Sequence[Dict[str, Any]],
) -> Optional[List[ColumnElement[Any]]]:
    if not sorts:
        return []

    fields_by_id = _field_map(fields)
    expressions: List[ColumnElement[Any]] = []
    for sort in sorts:
        field_id = str(sort.get("fieldId") or "")
        field = fields_by_id.get(field_id)
        if not field:
            return None
        field_type = str(field.get("type") or "")
        order = "desc" if str(sort.get("order") or "").lower() == "desc" else "asc"
        text_expr = _text_expression(field_id)
        empty = _empty_expression(field_id)
        empty_rank = case((empty, 1), else_=0)

        if field_type in _SORT_NUMBER_SQL_TYPES:
            value_expr = cast(text_expr, Float)
        elif field_type in _SORT_TEXT_SQL_TYPES:
            value_expr = func.lower(text_expr)
        else:
            return None

        if order == "desc":
            expressions.extend([empty_rank.desc(), value_expr.desc()])
        else:
            expressions.extend([empty_rank.asc(), value_expr.asc()])

    expressions.append(TableRecord.order_index.asc())
    expressions.append(TableRecord.id.asc())
    return expressions


async def query_records_db(
    db: AsyncSession,
    table_id: str,
    fields: Sequence[Dict[str, Any]],
    *,
    filters: Optional[Sequence[Dict[str, Any]]] = None,
    sorts: Optional[Sequence[Dict[str, Any]]] = None,
    offset: int = 0,
    limit: int = DEFAULT_QUERY_LIMIT,
    allowed_record_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    query_filters = list(filters or [])
    query_sorts = list(sorts or [])
    if len(query_filters) > MAX_FILTERS:
        raise ValueError(f"Too many filters (max {MAX_FILTERS})")
    if len(query_sorts) > MAX_SORTS:
        raise ValueError(f"Too many sorts (max {MAX_SORTS})")

    normalized_offset = max(0, int(offset))
    normalized_limit = max(1, min(MAX_QUERY_LIMIT, int(limit)))
    filter_expression = compile_filter_expression(query_filters, fields)
    sort_expressions = compile_sort_expressions(query_sorts, fields)
    sql_filter_applied = bool(query_filters) and filter_expression is not None
    sql_sort_applied = bool(query_sorts) and sort_expressions is not None

    conditions: List[ColumnElement[bool]] = [TableRecord.table_id == table_id]
    if allowed_record_ids is not None:
        normalized_allowed = [str(record_id) for record_id in allowed_record_ids]
        if not normalized_allowed:
            return {
                "records": [],
                "totalCount": 0,
                "offset": normalized_offset,
                "limit": normalized_limit,
                "hasMore": False,
                "nextOffset": None,
                "executionMode": "sql",
                "sqlFilterApplied": sql_filter_applied,
                "sqlSortApplied": sql_sort_applied,
                "databasePaged": True,
            }
        conditions.append(TableRecord.id.in_(normalized_allowed))
    if filter_expression is not None:
        conditions.append(filter_expression)

    database_paged = (
        (not query_filters or filter_expression is not None)
        and (not query_sorts or sort_expressions is not None)
        and _filters_support_exact_sql_paging(query_filters, fields)
        and _sorts_support_exact_sql_paging(query_sorts, fields)
    )

    statement = select(TableRecord).where(*conditions)
    if sort_expressions is not None and sort_expressions:
        statement = statement.order_by(*sort_expressions)
    else:
        statement = statement.order_by(
            TableRecord.order_index.asc(),
            TableRecord.id.asc(),
        )

    if database_paged:
        count_statement = (
            select(func.count())
            .select_from(TableRecord)
            .where(*conditions)
        )
        total_count = int((await db.execute(count_statement)).scalar() or 0)
        page_statement = statement.offset(normalized_offset).limit(normalized_limit)
        page_result = await db.execute(page_statement)
        raw_records: List[Dict[str, Any]] = []
        for row in page_result.scalars().all():
            record = dict(row.data or {})
            record["id"] = row.id
            raw_records.append(record)

        records = materialize_records(list(fields), raw_records)
        # Reuse the parity implementation on the already bounded page. The
        # conservative guards above guarantee this cannot reshuffle rows across
        # page boundaries.
        exact_page = query_records_in_memory(
            fields,
            records,
            filters=query_filters,
            sorts=query_sorts,
            offset=0,
            limit=normalized_limit,
        )["records"]
        next_offset = normalized_offset + len(exact_page)
        has_more = next_offset < total_count
        return {
            "records": exact_page,
            "totalCount": total_count,
            "offset": normalized_offset,
            "limit": normalized_limit,
            "hasMore": has_more,
            "nextOffset": next_offset if has_more else None,
            "executionMode": "sql",
            "sqlFilterApplied": sql_filter_applied,
            "sqlSortApplied": sql_sort_applied,
            "databasePaged": True,
        }

    result = await db.execute(statement)
    raw_records = []
    for row in result.scalars().all():
        record = dict(row.data or {})
        record["id"] = row.id
        raw_records.append(record)

    # Compatibility path: SQL may narrow candidates, then Python performs the
    # exact legacy-aware filter/sort before pagination.
    records = materialize_records(list(fields), raw_records)
    response = query_records_in_memory(
        fields,
        records,
        filters=query_filters,
        sorts=query_sorts,
        offset=normalized_offset,
        limit=normalized_limit,
    )
    if (not query_filters or sql_filter_applied) and (not query_sorts or sql_sort_applied):
        execution_mode = "sql"
    elif sql_filter_applied or sql_sort_applied:
        execution_mode = "hybrid"
    else:
        execution_mode = "memory"

    response.update(
        {
            "executionMode": execution_mode,
            "sqlFilterApplied": sql_filter_applied,
            "sqlSortApplied": sql_sort_applied,
            "databasePaged": False,
        }
    )
    return response
