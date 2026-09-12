from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any, AsyncGenerator, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, unquote

from sqlalchemy import Numeric, String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board import BoardCardOrder
from app.models.smart_table import TableField, TableRecord, TableView
from app.services.change_history import append_change_set, changed_fields, record_meta, record_version
from app.services.formula_engine import materialize_records
from app.services.member_field import (
    member_allows_multiple,
    normalize_member_value_for_field,
    workspace_member_options_for_table,
)
from app.services.record_query import (
    MAX_FILTERS,
    MAX_SORTS,
    compile_filter_expression,
    compile_sort_expressions,
    matches_filters,
    sort_records,
)
from app.services.row_permissions import (
    row_permission_restricts_user,
    sanitize_relation_values_for_user,
)
from app.services.workspace import permission_allows


UNASSIGNED_KEY = "__unassigned__"
DEFAULT_LANE_KEY = "__default__"
RANK_STEP = Decimal("1000000")
MIN_RANK_GAP = Decimal("0.000000000000000001")
MAX_BOARD_LIMIT = 200
DEFAULT_BOARD_LIMIT = 50
MAX_COMPAT_CANDIDATES = 5000
BOARD_GROUP_FIELD_TYPES = {"select", "text"}
BOARD_LANE_FIELD_TYPES = {"select", "member"}


class BoardError(ValueError):
    pass


class BoardConfigurationError(BoardError):
    pass


class BoardConflictError(BoardError):
    pass


class BoardQueryTooLargeError(BoardError):
    pass


class _BoardBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def publish(self, payload: Dict[str, Any]) -> None:
        async with self._lock:
            for queue in list(self._subscribers):
                queue.put_nowait(dict(payload))

    async def subscribe(self) -> AsyncGenerator[Dict[str, Any], None]:  # type: ignore[type-arg]
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)


board_broker = _BoardBroker()


async def publish_board_update(payload: Mapping[str, Any]) -> None:
    event = dict(payload)
    event.setdefault("updatedAt", datetime.now(timezone.utc).isoformat())
    await board_broker.publish(event)


def _field_dict(field: TableField) -> Dict[str, Any]:
    return {
        "id": field.id,
        "name": field.name,
        "type": field.type,
        "options": field.options,
        "property": field.property,
    }


def _field_map(fields: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(field.get("id")): dict(field)
        for field in fields
        if field.get("id") is not None
    }


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _primitive(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("id", "value", "label", "name", "userId", "user_id"):
            candidate = value.get(key)
            if candidate is not None and candidate != "":
                return str(candidate)
        return ""
    if isinstance(value, list):
        return _primitive(value[0]) if value else ""
    return str(value)


def _option_for_value(field: Mapping[str, Any], value: Any) -> Optional[Dict[str, Any]]:
    raw = _primitive(value).casefold()
    if not raw:
        return None
    for option in field.get("options") or []:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id") or "")
        label = str(option.get("label") or "")
        if raw in {option_id.casefold(), label.casefold()}:
            return dict(option)
    return None


def _descriptor(field: Mapping[str, Any], value: Any) -> Dict[str, Any]:
    if _is_empty(value):
        return {"key": UNASSIGNED_KEY, "label": "未分配", "value": None}

    field_type = str(field.get("type") or "")
    if field_type == "select":
        option = _option_for_value(field, value)
        if option:
            option_id = str(option.get("id") or option.get("label") or "")
            return {
                "key": f"select:{quote(option_id, safe='')}",
                "label": str(option.get("label") or option_id),
                "value": option_id,
            }

    if field_type == "member":
        member_id = _primitive(value)
        for option in field.get("options") or []:
            if isinstance(option, dict) and str(option.get("id")) == member_id:
                return {
                    "key": f"member:{quote(member_id, safe='')}",
                    "label": str(option.get("label") or member_id),
                    "value": member_id,
                }
        return {
            "key": f"member:{quote(member_id, safe='')}",
            "label": member_id,
            "value": member_id,
        }

    raw = _primitive(value)
    return {
        "key": f"value:{quote(raw, safe='')}",
        "label": raw,
        "value": raw,
    }


def _value_from_key(field: Mapping[str, Any], key: str) -> Any:
    if key == UNASSIGNED_KEY:
        return None
    if key.startswith("select:"):
        option_id = unquote(key.split(":", 1)[1])
        for option in field.get("options") or []:
            if not isinstance(option, dict):
                continue
            if str(option.get("id") or option.get("label") or "") == option_id:
                return option_id
        raise BoardConfigurationError("Board column/lane option no longer exists")
    if key.startswith("member:"):
        member_id = unquote(key.split(":", 1)[1])
        available = {
            str(option.get("id"))
            for option in field.get("options") or []
            if isinstance(option, dict) and option.get("id") is not None
        }
        if member_id not in available:
            raise BoardConfigurationError("Board lane member is not in this workspace")
        return member_id
    if key.startswith("value:"):
        return unquote(key.split(":", 1)[1])
    raise BoardConfigurationError("Invalid board column/lane key")


def _json_text(field_id: str):
    return TableRecord.data[field_id].as_string()


def _empty_condition(field_id: str):
    expression = _json_text(field_id)
    return or_(expression.is_(None), expression == "")


def _value_condition(field: Mapping[str, Any], key: str):
    field_id = str(field.get("id") or "")
    if not field_id:
        raise BoardConfigurationError("Board field is missing")
    if key == UNASSIGNED_KEY:
        return _empty_condition(field_id)

    value = _value_from_key(field, key)
    expression = func.lower(_json_text(field_id))
    if str(field.get("type") or "") == "select":
        option = _option_for_value(field, value)
        if option:
            values = {
                str(option.get("id") or "").casefold(),
                str(option.get("label") or "").casefold(),
            }
            values.discard("")
            return or_(*[expression == item for item in values])
    return expression == str(value).casefold()


def _visibility_condition(
    *,
    row_policy: Mapping[str, Any],
    table_permission: str,
    user_id: int,
):
    if not row_permission_restricts_user(dict(row_policy), table_permission):
        return None

    own = TableRecord.created_by_user_id == int(user_id)
    mode = str(row_policy.get("mode") or "all")
    if mode == "creator":
        return own
    if mode == "member_field":
        field_id = row_policy.get("memberFieldId")
        if not isinstance(field_id, str) or not field_id:
            return own
        # Member values are normalized to a scalar user ID or an array of user
        # IDs. Quoted-ID matching avoids 1 matching 11 and works on SQLite and
        # PostgreSQL JSON text casts.
        membership = cast(TableRecord.data[field_id], String).contains(
            json.dumps(str(user_id))
        )
        return or_(own, membership)
    return own


def _rank_expression():
    fallback = cast(TableRecord.order_index, Numeric(50, 20)) * RANK_STEP
    return func.coalesce(BoardCardOrder.rank, fallback)


def _cursor_signature(
    *,
    table_id: str,
    view_id: str,
    column_key: Optional[str],
    lane_key: Optional[str],
    filters: Sequence[Mapping[str, Any]],
    sorts: Sequence[Mapping[str, Any]],
) -> str:
    canonical = json.dumps(
        {
            "table": table_id,
            "view": view_id,
            "column": column_key,
            "lane": lane_key,
            "filters": list(filters),
            "sorts": list(sorts),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _encode_cursor(offset: int, signature: str) -> str:
    payload = json.dumps({"v": 1, "o": offset, "s": signature}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str], signature: str) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if payload.get("v") != 1 or payload.get("s") != signature:
            raise ValueError
        return max(0, int(payload.get("o", 0)))
    except Exception as exc:
        raise BoardConflictError("Board cursor is stale or invalid") from exc


async def _load_board_context(
    db: AsyncSession,
    table_id: str,
    view_id: str,
) -> Tuple[TableView, List[Dict[str, Any]], Dict[str, Any]]:
    view_result = await db.execute(
        select(TableView).where(
            TableView.table_id == table_id,
            TableView.id == view_id,
        )
    )
    view = view_result.scalars().first()
    if not view or str(view.type or "").lower() != "board":
        raise BoardConfigurationError("Kanban view not found")

    field_result = await db.execute(
        select(TableField)
        .where(TableField.table_id == table_id)
        .order_by(TableField.order_index, TableField.id)
    )
    field_rows = field_result.scalars().all()
    fields = [_field_dict(field) for field in field_rows]
    # Keep references to the returned field dictionaries so dynamic member
    # options hydrated below remain available to query/move validation.
    fields_by_id = {
        str(field["id"]): field
        for field in fields
        if field.get("id") is not None
    }

    raw_config = dict(view.config or {})
    raw_board = raw_config.get("boardConfig")
    board = dict(raw_board) if isinstance(raw_board, dict) else {}
    legacy_group = raw_config.get("groupConfig")
    if not board.get("groupFieldId") and isinstance(legacy_group, dict):
        board["groupFieldId"] = legacy_group.get("fieldId")
    board.setdefault("laneFieldId", None)
    board.setdefault("cardFieldIds", [])
    board.setdefault("cardOrder", "manual")
    board.setdefault("hideCompleted", False)
    board.setdefault("collapsedColumns", [])

    group_id = board.get("groupFieldId")
    group_field = fields_by_id.get(str(group_id or ""))
    if not group_field:
        raise BoardConfigurationError("Kanban needs a valid group field")
    if str(group_field.get("type") or "") not in BOARD_GROUP_FIELD_TYPES:
        raise BoardConfigurationError("Kanban group field must be select or text")

    lane_id = board.get("laneFieldId")
    if lane_id:
        lane_field = fields_by_id.get(str(lane_id))
        if not lane_field:
            raise BoardConfigurationError("Kanban lane field no longer exists")
        if str(lane_field.get("type") or "") not in BOARD_LANE_FIELD_TYPES:
            raise BoardConfigurationError("Kanban lane field must be member or select")
        if str(lane_field.get("type") or "") == "member" and member_allows_multiple(lane_field):
            raise BoardConfigurationError(
                "Kanban member swimlanes require a single-select member field"
            )
        member_options = await workspace_member_options_for_table(db, table_id)
        lane_field["options"] = member_options if lane_field.get("type") == "member" else lane_field.get("options")

    card_ids = board.get("cardFieldIds")
    if not isinstance(card_ids, list):
        raise BoardConfigurationError("cardFieldIds must be a list")
    valid_card_ids = [str(field_id) for field_id in card_ids if str(field_id) in fields_by_id]
    board["cardFieldIds"] = valid_card_ids[:12]
    board["groupFieldId"] = str(group_field["id"])
    board["laneFieldId"] = str(lane_id) if lane_id else None
    board["cardOrder"] = "manual"
    board["hideCompleted"] = bool(board.get("hideCompleted", False))
    collapsed = board.get("collapsedColumns")
    board["collapsedColumns"] = [str(value) for value in collapsed] if isinstance(collapsed, list) else []
    return view, fields, board


async def update_board_view_config(
    db: AsyncSession,
    *,
    table_id: str,
    view_id: str,
    group_field_id: str,
    lane_field_id: Optional[str] = None,
    card_field_ids: Optional[Sequence[str]] = None,
    hide_completed: bool = False,
    collapsed_columns: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    view_result = await db.execute(
        select(TableView)
        .where(TableView.table_id == table_id, TableView.id == view_id)
        .with_for_update()
    )
    view = view_result.scalars().first()
    if not view or str(view.type or "").lower() != "board":
        raise BoardConfigurationError("Kanban view not found")

    field_result = await db.execute(
        select(TableField).where(TableField.table_id == table_id)
    )
    fields = {_field.id: _field for _field in field_result.scalars().all()}
    group_field = fields.get(str(group_field_id))
    if not group_field or group_field.type not in BOARD_GROUP_FIELD_TYPES:
        raise BoardConfigurationError("Kanban group field must be select or text")

    normalized_lane: Optional[str] = None
    if lane_field_id:
        lane_field = fields.get(str(lane_field_id))
        if not lane_field or lane_field.type not in BOARD_LANE_FIELD_TYPES:
            raise BoardConfigurationError("Kanban lane field must be member or select")
        if lane_field.type == "member" and member_allows_multiple(lane_field):
            raise BoardConfigurationError(
                "Kanban member swimlanes require a single-select member field"
            )
        normalized_lane = lane_field.id

    normalized_cards = []
    for field_id in card_field_ids or []:
        normalized = str(field_id)
        if normalized not in fields:
            raise BoardConfigurationError(f"Card field {normalized} does not exist")
        if normalized not in normalized_cards:
            normalized_cards.append(normalized)
    if len(normalized_cards) > 12:
        raise BoardConfigurationError("Kanban cards support at most 12 configured fields")

    config = dict(view.config or {})
    config["groupConfig"] = {
        **(dict(config.get("groupConfig") or {}) if isinstance(config.get("groupConfig"), dict) else {}),
        "fieldId": group_field.id,
        "order": str((config.get("groupConfig") or {}).get("order") or "asc")
        if isinstance(config.get("groupConfig"), dict)
        else "asc",
    }
    config["boardConfig"] = {
        "groupFieldId": group_field.id,
        "laneFieldId": normalized_lane,
        "cardFieldIds": normalized_cards,
        "cardOrder": "manual",
        "hideCompleted": bool(hide_completed),
        "collapsedColumns": list(dict.fromkeys(str(value) for value in (collapsed_columns or []))),
    }
    view.config = config
    await db.commit()
    return {
        "id": view.id,
        "name": view.name,
        "type": view.type,
        "config": config,
        "boardConfig": config["boardConfig"],
    }


def _seed_descriptors(field: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    field_type = str(field.get("type") or "")
    if field_type in {"select", "member"}:
        for option in field.get("options") or []:
            if not isinstance(option, dict):
                continue
            value = option.get("id")
            descriptor = _descriptor(field, value)
            result[descriptor["key"]] = {**descriptor, "count": 0}
    result[UNASSIGNED_KEY] = {
        "key": UNASSIGNED_KEY,
        "label": "未分配",
        "value": None,
        "count": 0,
    }
    return result


def _ordered_descriptors(
    field: Mapping[str, Any],
    descriptors: Mapping[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    option_order: Dict[str, int] = {}
    for index, option in enumerate(field.get("options") or []):
        if not isinstance(option, dict):
            continue
        descriptor = _descriptor(field, option.get("id"))
        option_order[descriptor["key"]] = index

    def key(item: Dict[str, Any]):
        if item["key"] == UNASSIGNED_KEY:
            return (2, 0, "")
        if item["key"] in option_order:
            return (0, option_order[item["key"]], "")
        return (1, 0, str(item.get("label") or "").casefold())

    return sorted((dict(item) for item in descriptors.values()), key=key)


async def _compat_records(
    db: AsyncSession,
    *,
    table_id: str,
    view_id: str,
    fields: Sequence[Dict[str, Any]],
    conditions: Sequence[Any],
    filters: Sequence[Dict[str, Any]],
    sorts: Sequence[Dict[str, Any]],
    user_id: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], bool]:
    result = await db.execute(
        select(TableRecord)
        .where(*conditions)
        .order_by(TableRecord.order_index, TableRecord.id)
        .limit(MAX_COMPAT_CANDIDATES + 1)
    )
    rows = result.scalars().all()
    if len(rows) > MAX_COMPAT_CANDIDATES:
        raise BoardQueryTooLargeError(
            "This filter/sort cannot be safely paged for a large Kanban. "
            "Use database-sortable fields or narrow the filters."
        )

    row_by_id = {str(row.id): row for row in rows}
    raw = [{"id": row.id, **dict(row.data or {})} for row in rows]
    materialized = materialize_records(list(fields), raw)
    matched = [record for record in materialized if matches_filters(record, filters, fields)]
    if sorts:
        matched = sort_records(matched, sorts, fields)
    matched = await sanitize_relation_values_for_user(
        db,
        fields,
        matched,
        user_id=user_id,
    )
    matched_ids = [str(record["id"]) for record in matched]
    order_result = await db.execute(
        select(BoardCardOrder).where(
            BoardCardOrder.table_id == table_id,
            BoardCardOrder.view_id == view_id,
            BoardCardOrder.record_id.in_(matched_ids or ["__none__"]),
        )
    )
    order_by_record = {str(row.record_id): row for row in order_result.scalars().all()}
    state_by_record: Dict[str, Dict[str, Any]] = {}
    for record_id in matched_ids:
        row = row_by_id.get(record_id)
        if row is None:
            continue
        order = order_by_record.get(record_id)
        state_by_record[record_id] = {
            "rank": (
                Decimal(order.rank)
                if order is not None
                else Decimal(int(row.order_index or 0)) * RANK_STEP
            ),
            "orderRevision": int(order.revision or 0) if order is not None else 0,
            "recordVersion": int(row.version or 1),
        }
    return matched, state_by_record, False


async def query_board(
    db: AsyncSession,
    *,
    table_id: str,
    view_id: str,
    user_id: int,
    table_permission: str,
    row_policy: Mapping[str, Any],
    column_key: Optional[str] = None,
    lane_key: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = DEFAULT_BOARD_LIMIT,
    filters: Optional[Sequence[Mapping[str, Any]]] = None,
    sorts: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    _view, fields, board = await _load_board_context(db, table_id, view_id)
    fields_by_id = _field_map(fields)
    group_field = fields_by_id[board["groupFieldId"]]
    lane_field = fields_by_id.get(str(board.get("laneFieldId") or ""))

    safe_filters = [dict(item) for item in (filters or []) if isinstance(item, Mapping)]
    safe_sorts = [dict(item) for item in (sorts or []) if isinstance(item, Mapping)]
    if len(safe_filters) != len(filters or []):
        raise BoardError("Every Kanban filter must be an object")
    if len(safe_sorts) != len(sorts or []):
        raise BoardError("Every Kanban sort must be an object")
    if len(safe_filters) > MAX_FILTERS:
        raise BoardError(f"Too many Kanban filters (max {MAX_FILTERS})")
    if len(safe_sorts) > MAX_SORTS:
        raise BoardError(f"Too many Kanban sorts (max {MAX_SORTS})")

    normalized_limit = max(1, min(MAX_BOARD_LIMIT, int(limit)))
    signature = _cursor_signature(
        table_id=table_id,
        view_id=view_id,
        column_key=column_key,
        lane_key=lane_key,
        filters=safe_filters,
        sorts=safe_sorts,
    )
    offset = _decode_cursor(cursor, signature)

    conditions: List[Any] = [TableRecord.table_id == table_id]
    visibility = _visibility_condition(
        row_policy=row_policy,
        table_permission=table_permission,
        user_id=user_id,
    )
    if visibility is not None:
        conditions.append(visibility)

    filter_expression = compile_filter_expression(safe_filters, fields)
    sort_expressions = compile_sort_expressions(safe_sorts, fields)
    sql_supported = (
        (not safe_filters or filter_expression is not None)
        and (not safe_sorts or sort_expressions is not None)
    )
    if filter_expression is not None:
        conditions.append(filter_expression)

    if not sql_supported:
        records, order_state_by_record, database_paged = await _compat_records(
            db,
            table_id=table_id,
            view_id=view_id,
            fields=fields,
            conditions=conditions,
            filters=safe_filters,
            sorts=safe_sorts,
            user_id=user_id,
        )
        columns = _seed_descriptors(group_field)
        lanes = _seed_descriptors(lane_field) if lane_field else {}
        cells: Dict[Tuple[str, Optional[str]], int] = {}
        cell_records: List[Dict[str, Any]] = []
        for record in records:
            column = _descriptor(group_field, record.get(group_field["id"]))
            lane = _descriptor(lane_field, record.get(lane_field["id"])) if lane_field else None
            columns.setdefault(column["key"], {**column, "count": 0})["count"] += 1
            if lane:
                lanes.setdefault(lane["key"], {**lane, "count": 0})["count"] += 1
            cell_key = (column["key"], lane["key"] if lane else None)
            cells[cell_key] = cells.get(cell_key, 0) + 1
            if column_key and column["key"] != column_key:
                continue
            if lane_field and lane_key and (not lane or lane["key"] != lane_key):
                continue
            cell_records.append(record)
        if not safe_sorts:
            cell_records.sort(
                key=lambda item: (
                    order_state_by_record.get(
                        str(item.get("id")),
                        {"rank": Decimal(0)},
                    )["rank"],
                    str(item.get("id")),
                )
            )
        page = cell_records[offset : offset + normalized_limit]
        next_offset = offset + len(page)
        has_more = next_offset < len(cell_records)
        cards = []
        for record in page:
            state = order_state_by_record.get(str(record.get("id"))) or {
                "rank": Decimal(0),
                "orderRevision": 0,
                "recordVersion": 1,
            }
            cards.append(
                {
                    "record": record,
                    "rank": str(state["rank"]),
                    "orderRevision": int(state["orderRevision"]),
                    "recordVersion": int(state["recordVersion"]),
                }
            )
        return {
            "viewId": view_id,
            "boardConfig": board,
            "columns": _ordered_descriptors(group_field, columns),
            "lanes": _ordered_descriptors(lane_field, lanes) if lane_field else [],
            "cells": [
                {"columnKey": column, "laneKey": lane, "count": count}
                for (column, lane), count in cells.items()
            ],
            "cards": cards,
            "pageInfo": {
                "totalCount": len(cell_records),
                "hasMore": has_more,
                "nextCursor": _encode_cursor(next_offset, signature) if has_more else None,
                "databasePaged": database_paged,
                "manualOrderActive": not bool(safe_sorts),
            },
        }

    group_expr = _json_text(group_field["id"])
    lane_expr = _json_text(lane_field["id"]) if lane_field else None
    grouping = [group_expr] + ([lane_expr] if lane_expr is not None else [])
    count_result = await db.execute(
        select(*grouping, func.count())
        .where(*conditions)
        .group_by(*grouping)
    )

    columns = _seed_descriptors(group_field)
    lanes = _seed_descriptors(lane_field) if lane_field else {}
    cells: Dict[Tuple[str, Optional[str]], int] = {}
    for row in count_result.all():
        raw_column = row[0]
        raw_lane = row[1] if lane_field else None
        count = int(row[-1] or 0)
        column = _descriptor(group_field, raw_column)
        lane = _descriptor(lane_field, raw_lane) if lane_field else None
        columns.setdefault(column["key"], {**column, "count": 0})["count"] += count
        if lane:
            lanes.setdefault(lane["key"], {**lane, "count": 0})["count"] += count
        cell = (column["key"], lane["key"] if lane else None)
        cells[cell] = cells.get(cell, 0) + count

    cards: List[Dict[str, Any]] = []
    total_count = 0
    has_more = False
    next_cursor: Optional[str] = None
    if column_key:
        card_conditions = [*conditions, _value_condition(group_field, column_key)]
        if lane_field and lane_key:
            card_conditions.append(_value_condition(lane_field, lane_key))
        count_statement = select(func.count()).select_from(TableRecord).where(*card_conditions)
        total_count = int((await db.execute(count_statement)).scalar() or 0)

        join_condition = and_(
            BoardCardOrder.table_id == TableRecord.table_id,
            BoardCardOrder.view_id == view_id,
            BoardCardOrder.record_id == TableRecord.id,
        )
        statement = (
            select(TableRecord, BoardCardOrder.rank, BoardCardOrder.revision)
            .outerjoin(BoardCardOrder, join_condition)
            .where(*card_conditions)
        )
        if safe_sorts and sort_expressions:
            statement = statement.order_by(*sort_expressions, _rank_expression(), TableRecord.id)
        else:
            statement = statement.order_by(_rank_expression(), TableRecord.id)
        statement = statement.offset(offset).limit(normalized_limit)
        page_rows = (await db.execute(statement)).all()
        raw_records = [
            {"id": record.id, **dict(record.data or {})}
            for record, _rank, _revision in page_rows
        ]
        materialized = materialize_records(fields, raw_records)
        sanitized = await sanitize_relation_values_for_user(
            db,
            fields,
            materialized,
            user_id=user_id,
        )
        sanitized_by_id = {str(record["id"]): record for record in sanitized}
        for record, rank, revision in page_rows:
            cards.append(
                {
                    "record": sanitized_by_id.get(str(record.id), {"id": record.id}),
                    "rank": str(rank) if rank is not None else None,
                    "orderRevision": int(revision or 0),
                    "recordVersion": int(record.version or 1),
                }
            )
        next_offset = offset + len(cards)
        has_more = next_offset < total_count
        next_cursor = _encode_cursor(next_offset, signature) if has_more else None

    return {
        "viewId": view_id,
        "boardConfig": board,
        "columns": _ordered_descriptors(group_field, columns),
        "lanes": _ordered_descriptors(lane_field, lanes) if lane_field else [],
        "cells": [
            {"columnKey": column, "laneKey": lane, "count": count}
            for (column, lane), count in cells.items()
        ],
        "cards": cards,
        "pageInfo": {
            "totalCount": total_count,
            "hasMore": has_more,
            "nextCursor": next_cursor,
            "databasePaged": True,
            "manualOrderActive": not bool(safe_sorts),
        },
    }


async def _record_order_state(
    db: AsyncSession,
    *,
    table_id: str,
    view_id: str,
    record: TableRecord,
    lock: bool = False,
) -> Tuple[Decimal, Optional[BoardCardOrder]]:
    statement = select(BoardCardOrder).where(
        BoardCardOrder.table_id == table_id,
        BoardCardOrder.view_id == view_id,
        BoardCardOrder.record_id == record.id,
    )
    if lock:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    order = result.scalars().first()
    return (
        Decimal(order.rank) if order else Decimal(int(record.order_index or 0)) * RANK_STEP,
        order,
    )


def _record_cell_keys(
    record: TableRecord,
    *,
    group_field: Mapping[str, Any],
    lane_field: Optional[Mapping[str, Any]],
) -> Tuple[str, Optional[str]]:
    data = dict(record.data or {})
    column = _descriptor(group_field, data.get(str(group_field["id"])))
    lane = _descriptor(lane_field, data.get(str(lane_field["id"]))) if lane_field else None
    return column["key"], lane["key"] if lane else None


async def _load_neighbor(
    db: AsyncSession,
    *,
    table_id: str,
    record_id: Optional[str],
) -> Optional[TableRecord]:
    if not record_id:
        return None
    result = await db.execute(
        select(TableRecord)
        .where(TableRecord.table_id == table_id, TableRecord.id == record_id)
        .with_for_update()
    )
    return result.scalars().first()


async def _rebalance_cell(
    db: AsyncSession,
    *,
    table_id: str,
    view_id: str,
    group_field: Mapping[str, Any],
    lane_field: Optional[Mapping[str, Any]],
    column_key: str,
    lane_key: Optional[str],
    exclude_record_id: Optional[str] = None,
) -> None:
    conditions: List[Any] = [
        TableRecord.table_id == table_id,
        _value_condition(group_field, column_key),
    ]
    if lane_field and lane_key:
        conditions.append(_value_condition(lane_field, lane_key))
    if exclude_record_id:
        conditions.append(TableRecord.id != exclude_record_id)

    join_condition = and_(
        BoardCardOrder.table_id == TableRecord.table_id,
        BoardCardOrder.view_id == view_id,
        BoardCardOrder.record_id == TableRecord.id,
    )
    rows = (
        await db.execute(
            select(TableRecord, BoardCardOrder)
            .outerjoin(BoardCardOrder, join_condition)
            .where(*conditions)
        )
    ).all()
    rows = sorted(
        rows,
        key=lambda pair: (
            Decimal(pair[1].rank)
            if pair[1] is not None
            else Decimal(int(pair[0].order_index or 0)) * RANK_STEP,
            pair[0].id,
        ),
    )
    for index, (record, order) in enumerate(rows, start=1):
        rank = Decimal(index) * RANK_STEP
        if order is None:
            db.add(
                BoardCardOrder(
                    table_id=table_id,
                    view_id=view_id,
                    record_id=record.id,
                    rank=rank,
                    revision=1,
                )
            )
        else:
            order.rank = rank
            order.revision = int(order.revision or 0) + 1
    await db.flush()


async def _rank_for_target_position(
    db: AsyncSession,
    *,
    table_id: str,
    view_id: str,
    group_field: Mapping[str, Any],
    lane_field: Optional[Mapping[str, Any]],
    column_key: str,
    lane_key: Optional[str],
    moving_record_id: str,
    before_record: Optional[TableRecord],
    after_record: Optional[TableRecord],
) -> Decimal:
    async def effective(record: Optional[TableRecord]) -> Optional[Decimal]:
        if record is None:
            return None
        rank, _ = await _record_order_state(
            db,
            table_id=table_id,
            view_id=view_id,
            record=record,
            lock=True,
        )
        return rank

    before_rank = await effective(before_record)
    after_rank = await effective(after_record)
    if before_rank is not None and after_rank is not None:
        if before_rank >= after_rank or after_rank - before_rank <= MIN_RANK_GAP:
            await _rebalance_cell(
                db,
                table_id=table_id,
                view_id=view_id,
                group_field=group_field,
                lane_field=lane_field,
                column_key=column_key,
                lane_key=lane_key,
                exclude_record_id=moving_record_id,
            )
            before_rank = await effective(before_record)
            after_rank = await effective(after_record)
        if before_rank is None or after_rank is None or before_rank >= after_rank:
            raise BoardConflictError("Target Kanban order changed; refresh and retry")
        with localcontext() as context:
            context.prec = 50
            return (before_rank + after_rank) / Decimal(2)
    if before_rank is not None:
        return before_rank + RANK_STEP
    if after_rank is not None:
        return after_rank - RANK_STEP

    conditions: List[Any] = [
        TableRecord.table_id == table_id,
        TableRecord.id != moving_record_id,
        _value_condition(group_field, column_key),
    ]
    if lane_field and lane_key:
        conditions.append(_value_condition(lane_field, lane_key))
    join_condition = and_(
        BoardCardOrder.table_id == TableRecord.table_id,
        BoardCardOrder.view_id == view_id,
        BoardCardOrder.record_id == TableRecord.id,
    )
    last = (
        await db.execute(
            select(_rank_expression())
            .select_from(TableRecord)
            .outerjoin(BoardCardOrder, join_condition)
            .where(*conditions)
            .order_by(_rank_expression().desc(), TableRecord.id.desc())
            .limit(1)
        )
    ).scalar()
    return (Decimal(last) if last is not None else Decimal(0)) + RANK_STEP


async def move_board_card(
    db: AsyncSession,
    *,
    table_id: str,
    view_id: str,
    record_id: str,
    target_column_key: str,
    user_id: int,
    target_lane_key: Optional[str] = None,
    before_record_id: Optional[str] = None,
    after_record_id: Optional[str] = None,
    expected_record_version: Optional[int] = None,
    expected_order_revision: Optional[int] = None,
) -> Dict[str, Any]:
    _view, fields, board = await _load_board_context(db, table_id, view_id)
    fields_by_id = _field_map(fields)
    group_field = fields_by_id[board["groupFieldId"]]
    lane_field = fields_by_id.get(str(board.get("laneFieldId") or ""))

    record_result = await db.execute(
        select(TableRecord)
        .where(TableRecord.table_id == table_id, TableRecord.id == record_id)
        .with_for_update()
    )
    record = record_result.scalars().first()
    if not record:
        raise BoardConflictError("Record not found or no access")
    current_version = record_version(record)
    if expected_record_version is not None and int(expected_record_version) != current_version:
        raise BoardConflictError("Record changed; refresh the Kanban and retry")

    current_rank, order = await _record_order_state(
        db,
        table_id=table_id,
        view_id=view_id,
        record=record,
        lock=True,
    )
    current_revision = int(order.revision or 0) if order else 0
    if expected_order_revision is not None and int(expected_order_revision) != current_revision:
        raise BoardConflictError("Card order changed; refresh the Kanban and retry")

    if before_record_id == record_id or after_record_id == record_id:
        raise BoardConflictError("A card cannot be its own ordering anchor")
    before_record = await _load_neighbor(db, table_id=table_id, record_id=before_record_id)
    after_record = await _load_neighbor(db, table_id=table_id, record_id=after_record_id)
    if before_record_id and before_record is None:
        raise BoardConflictError("The previous card no longer exists")
    if after_record_id and after_record is None:
        raise BoardConflictError("The next card no longer exists")

    target_column_value = _value_from_key(group_field, target_column_key)
    current_column_key, current_lane_key = _record_cell_keys(
        record,
        group_field=group_field,
        lane_field=lane_field,
    )
    resolved_lane_key = target_lane_key if target_lane_key is not None else current_lane_key
    target_lane_value: Any = None
    if lane_field:
        resolved_lane_key = resolved_lane_key or UNASSIGNED_KEY
        target_lane_value = _value_from_key(lane_field, resolved_lane_key)
        if str(lane_field.get("type") or "") == "member":
            target_lane_value = await normalize_member_value_for_field(
                db,
                table_id,
                str(lane_field["id"]),
                target_lane_value,
            )

    for neighbor in (before_record, after_record):
        if neighbor is None:
            continue
        neighbor_column, neighbor_lane = _record_cell_keys(
            neighbor,
            group_field=group_field,
            lane_field=lane_field,
        )
        if neighbor_column != target_column_key:
            raise BoardConflictError("Ordering anchor is no longer in the target column")
        if lane_field and neighbor_lane != resolved_lane_key:
            raise BoardConflictError("Ordering anchor is no longer in the target swimlane")

    next_rank = await _rank_for_target_position(
        db,
        table_id=table_id,
        view_id=view_id,
        group_field=group_field,
        lane_field=lane_field,
        column_key=target_column_key,
        lane_key=resolved_lane_key,
        moving_record_id=record_id,
        before_record=before_record,
        after_record=after_record,
    )

    before_data = dict(record.data or {})
    next_data = dict(before_data)
    next_data[str(group_field["id"])] = target_column_value
    if lane_field and target_lane_key is not None:
        next_data[str(lane_field["id"])] = target_lane_value
    changed = changed_fields(before_data, next_data)
    before_meta = record_meta(record)
    if changed:
        record.data = next_data
        record.version = current_version + 1
        await append_change_set(
            db,
            table_id=table_id,
            actor_id=user_id,
            actor_type="user",
            operation="update",
            source="kanban",
            summary=f"Move record {record_id} on Kanban",
            items=[
                {
                    "table_id": table_id,
                    "entity_type": "record",
                    "entity_id": record_id,
                    "before_data": before_data,
                    "after_data": dict(next_data),
                    "before_meta": before_meta,
                    "after_meta": record_meta(record),
                    "version_before": current_version,
                    "version_after": record.version,
                    "changed_fields": changed,
                }
            ],
        )

    if order is None:
        order = BoardCardOrder(
            table_id=table_id,
            view_id=view_id,
            record_id=record_id,
            rank=next_rank,
            revision=1,
        )
        db.add(order)
    else:
        order.rank = next_rank
        order.revision = current_revision + 1

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await db.refresh(record)
    await db.refresh(order)

    return {
        "record": {"id": record.id, **dict(record.data or {})},
        "recordVersion": int(record.version or 1),
        "rank": str(order.rank),
        "orderRevision": int(order.revision or 1),
        "previousRank": str(current_rank),
        "previousColumnKey": current_column_key,
        "previousLaneKey": current_lane_key,
        "columnKey": target_column_key,
        "laneKey": resolved_lane_key,
    }
