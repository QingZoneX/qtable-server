from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Float, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField, TableRecord


AGGREGATIONS = {"sum", "count", "avg", "max", "min"}

_TABLE_JSON_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _dimension_key(value: Any) -> str:
    """Render structured dimensions without leaking raw JSON/object syntax.

    Member fields are commonly stored as a list of {id, name} objects. SQL
    aggregation can still group the stored JSON efficiently; in-memory paths
    should emit the same human-readable label shape.
    """
    if value is None:
        return "—"
    if isinstance(value, dict):
        for key in ("name", "label", "title", "id"):
            candidate = value.get(key)
            if candidate not in (None, ""):
                return str(candidate)
        return str(value)
    if isinstance(value, list):
        labels = [_dimension_key(item) for item in value if item is not None]
        labels = [item for item in labels if item != "—"]
        return ", ".join(labels) if labels else "—"
    return str(value)


@dataclass(frozen=True)
class MetricSpec:
    field_id: Optional[str]
    aggregation: str


def _normalize_metric(config: Dict[str, Any]) -> MetricSpec:
    metric = config.get("metric") if isinstance(config.get("metric"), dict) else {}
    aggregation = str(metric.get("aggregation") or "count").lower()
    if aggregation not in AGGREGATIONS:
        aggregation = "count"
    field_id = metric.get("fieldId")
    if field_id is not None and not isinstance(field_id, str):
        field_id = None
    if aggregation != "count" and not field_id:
        aggregation = "count"
    return MetricSpec(field_id=field_id, aggregation=aggregation)


def _dialect_extract_expr(dialect: str, json_col, field_id: str):
    if dialect == "postgresql":
        return func.jsonb_extract_path_text(json_col, field_id)
    return func.json_extract(json_col, f"$.{field_id}")


def _build_filter_predicates(
    dialect: str, json_col, filters: List[Dict[str, Any]]
):
    preds = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        field_id = f.get("fieldId")
        op = f.get("operator")
        value = f.get("value")
        if not isinstance(field_id, str) or not isinstance(op, str):
            continue
        expr = _dialect_extract_expr(dialect, json_col, field_id)
        op_l = op.lower()
        if op_l in {"=", "eq"}:
            preds.append(expr == str(value) if value is not None else expr.is_(None))
        elif op_l in {"!=", "neq"}:
            preds.append(expr != str(value) if value is not None else expr.is_not(None))
        elif op_l in {">", "gt"}:
            preds.append(cast(expr, Float) > float(value or 0))
        elif op_l in {">=", "gte"}:
            preds.append(cast(expr, Float) >= float(value or 0))
        elif op_l in {"<", "lt"}:
            preds.append(cast(expr, Float) < float(value or 0))
        elif op_l in {"<=", "lte"}:
            preds.append(cast(expr, Float) <= float(value or 0))
        elif op_l in {"contains"}:
            preds.append(expr.ilike(f"%{value}%"))
        elif op_l in {"in"} and isinstance(value, list):
            preds.append(expr.in_([str(v) for v in value]))
        elif op_l == "before":
            preds.append(expr < str(value)[:10])
        elif op_l == "after":
            preds.append(expr > str(value)[:10])
    return preds


def _apply_aggregation(metric: MetricSpec, expr_value):
    if metric.aggregation == "sum":
        return func.sum(cast(expr_value, Float))
    if metric.aggregation == "avg":
        return func.avg(cast(expr_value, Float))
    if metric.aggregation == "max":
        return func.max(cast(expr_value, Float))
    if metric.aggregation == "min":
        return func.min(cast(expr_value, Float))
    return func.count()


async def compute_widget_data_db(
    db: AsyncSession,
    config: Dict[str, Any],
    allowed_record_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    table_id = config.get("tableId")
    if not isinstance(table_id, str) or not table_id:
        return {"rows": [], "metric": {"aggregation": "count"}}

    dialect = db.bind.dialect.name if db.bind else "postgresql"
    dimension_field_id = config.get("dimensionFieldId")
    dimension_field_id = dimension_field_id if isinstance(dimension_field_id, str) else None
    metric = _normalize_metric(config)
    filters = config.get("filters")
    filters = filters if isinstance(filters, list) else []
    sorts = config.get("sort")
    sorts = sorts if isinstance(sorts, dict) else {}
    limit = config.get("limit")
    limit_n = int(limit) if isinstance(limit, int) or isinstance(limit, float) else 200
    limit_n = max(1, min(limit_n, 1000))

    json_col = TableRecord.data
    where_preds = _build_filter_predicates(dialect, json_col, filters)

    normalized_allowed_ids = (
        [str(record_id) for record_id in allowed_record_ids]
        if allowed_record_ids is not None
        else None
    )
    if normalized_allowed_ids == []:
        if dimension_field_id:
            return {
                "rows": [],
                "dimensionFieldId": dimension_field_id,
                "metric": {"fieldId": metric.field_id, "aggregation": metric.aggregation},
            }
        return {
            "rows": [{"value": 0.0}],
            "dimensionFieldId": None,
            "metric": {"fieldId": metric.field_id, "aggregation": metric.aggregation},
        }

    if dialect == "sqlite":
        sqlite_stmt = select(TableRecord.data).where(TableRecord.table_id == table_id)
        if normalized_allowed_ids is not None:
            sqlite_stmt = sqlite_stmt.where(TableRecord.id.in_(normalized_allowed_ids))
        result = await db.execute(sqlite_stmt)
        raw_rows = result.all()
        records: List[Dict[str, Any]] = []
        for r in raw_rows:
            payload = r[0]
            if isinstance(payload, dict):
                records.append(payload)

        def passes_filters(record: Dict[str, Any]) -> bool:
            for f in filters:
                if not isinstance(f, dict):
                    continue
                fid = f.get("fieldId")
                op = f.get("operator")
                value = f.get("value")
                if not isinstance(fid, str) or not isinstance(op, str):
                    continue
                cell = record.get(fid)
                op_l = op.lower()
                if op_l in {"=", "eq"} and not (str(cell) if cell is not None else None) == (str(value) if value is not None else None):
                    return False
                if op_l in {"!=", "neq"} and (str(cell) if cell is not None else None) == (str(value) if value is not None else None):
                    return False
                if op_l in {"contains"} and (value is not None) and (str(value) not in str(cell or "")):
                    return False
                if op_l in {"in"} and isinstance(value, list) and (str(cell) if cell is not None else None) not in [str(v) for v in value]:
                    return False
                if op_l == "before" and not (str(cell or "")[:10] < str(value or "")[:10]):
                    return False
                if op_l == "after" and not (str(cell or "")[:10] > str(value or "")[:10]):
                    return False
                if op_l in {">", "gt"} and not (float(cell or 0) > float(value or 0)):
                    return False
                if op_l in {">=", "gte"} and not (float(cell or 0) >= float(value or 0)):
                    return False
                if op_l in {"<", "lt"} and not (float(cell or 0) < float(value or 0)):
                    return False
                if op_l in {"<=", "lte"} and not (float(cell or 0) <= float(value or 0)):
                    return False
            return True

        filtered = [r for r in records if passes_filters(r)]

        def metric_value(record: Dict[str, Any]) -> float:
            if metric.aggregation == "count" or not metric.field_id:
                return 1.0
            v = record.get(metric.field_id)
            try:
                return float(v)
            except Exception:
                return 0.0

        if dimension_field_id:
            buckets: Dict[str, List[float]] = {}
            for r in filtered:
                key = r.get(dimension_field_id)
                buckets.setdefault(_dimension_key(key), []).append(metric_value(r))

            rows: List[Dict[str, Any]] = []
            for k, values in buckets.items():
                if metric.aggregation == "sum":
                    agg = sum(values)
                elif metric.aggregation == "avg":
                    agg = sum(values) / len(values) if values else 0
                elif metric.aggregation == "max":
                    agg = max(values) if values else 0
                elif metric.aggregation == "min":
                    agg = min(values) if values else 0
                else:
                    agg = float(len(values))
                rows.append({"dimension": k, "value": float(agg)})

            order = str(sorts.get("order") or "desc").lower()
            order_by = str(sorts.get("by") or "value").lower()
            if order_by == "dimension":
                rows.sort(key=lambda x: str(x.get("dimension") or ""), reverse=order != "asc")
            else:
                rows.sort(key=lambda x: float(x.get("value") or 0), reverse=order != "asc")
            rows = rows[:limit_n]
            return {
                "rows": rows,
                "dimensionFieldId": dimension_field_id,
                "metric": {"fieldId": metric.field_id, "aggregation": metric.aggregation},
            }

        values = [metric_value(r) for r in filtered]
        if metric.aggregation == "sum":
            agg = sum(values)
        elif metric.aggregation == "avg":
            agg = sum(values) / len(values) if values else 0
        elif metric.aggregation == "max":
            agg = max(values) if values else 0
        elif metric.aggregation == "min":
            agg = min(values) if values else 0
        else:
            agg = float(len(values))
        return {
            "rows": [{"value": float(agg)}],
            "dimensionFieldId": None,
            "metric": {"fieldId": metric.field_id, "aggregation": metric.aggregation},
        }

    metric_expr = (
        _dialect_extract_expr(dialect, json_col, metric.field_id)
        if metric.field_id
        else None
    )
    agg_expr = _apply_aggregation(metric, metric_expr) if metric_expr is not None else func.count()

    if dimension_field_id:
        dim_expr = _dialect_extract_expr(dialect, json_col, dimension_field_id)
        stmt = (
            select(
                dim_expr.label("dimension"),
                agg_expr.label("value"),
            )
            .where(TableRecord.table_id == table_id)
            .group_by(dim_expr)
        )
        if normalized_allowed_ids is not None:
            stmt = stmt.where(TableRecord.id.in_(normalized_allowed_ids))
        for pred in where_preds:
            stmt = stmt.where(pred)
        order = str(sorts.get("order") or "desc").lower()
        order_by = str(sorts.get("by") or "value").lower()
        if order_by == "dimension":
            stmt = stmt.order_by(dim_expr.asc() if order == "asc" else dim_expr.desc())
        else:
            stmt = stmt.order_by(agg_expr.asc() if order == "asc" else agg_expr.desc())
        stmt = stmt.limit(limit_n)
        result = await db.execute(stmt)
        rows = [{"dimension": r[0], "value": float(r[1] or 0)} for r in result.all()]
        return {
            "rows": rows,
            "dimensionFieldId": dimension_field_id,
            "metric": {"fieldId": metric.field_id, "aggregation": metric.aggregation},
        }

    stmt = select(agg_expr.label("value")).where(TableRecord.table_id == table_id)
    if normalized_allowed_ids is not None:
        stmt = stmt.where(TableRecord.id.in_(normalized_allowed_ids))
    for pred in where_preds:
        stmt = stmt.where(pred)
    result = await db.execute(stmt)
    value = result.scalar()
    return {
        "rows": [{"value": float(value or 0)}],
        "dimensionFieldId": None,
        "metric": {"fieldId": metric.field_id, "aggregation": metric.aggregation},
    }


def compute_widget_data_records(
    config: Dict[str, Any],
    records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute a widget from an already-authorized/sanitized record set.

    Used when a widget touches relation fields: relation visibility is
    user-specific, so SQL aggregation over raw JSON IDs would bypass the
    target table's row permissions.
    """
    dimension_field_id = config.get("dimensionFieldId")
    dimension_field_id = (
        dimension_field_id if isinstance(dimension_field_id, str) else None
    )
    metric = _normalize_metric(config)
    filters = config.get("filters")
    filters = filters if isinstance(filters, list) else []
    sort = config.get("sort")
    sort = sort if isinstance(sort, dict) else {}
    limit = config.get("limit")
    limit_n = int(limit) if isinstance(limit, (int, float)) else 200
    limit_n = max(1, min(limit_n, 1000))

    def passes_filters(record: Dict[str, Any]) -> bool:
        for item in filters:
            if not isinstance(item, dict):
                continue
            field_id = item.get("fieldId")
            operator = item.get("operator")
            value = item.get("value")
            if not isinstance(field_id, str) or not isinstance(operator, str):
                continue

            cell = record.get(field_id)
            op = operator.lower()
            if op in {"=", "eq"} and cell != value:
                return False
            if op in {"!=", "neq"} and cell == value:
                return False
            if op == "contains" and value is not None:
                if isinstance(cell, list):
                    if str(value) not in {str(part) for part in cell}:
                        return False
                elif str(value) not in str(cell or ""):
                    return False
            if op == "in" and isinstance(value, list):
                allowed_values = {str(part) for part in value}
                if isinstance(cell, list):
                    if not any(str(part) in allowed_values for part in cell):
                        return False
                elif str(cell) not in allowed_values:
                    return False
            if op == "before" and not (str(cell or "")[:10] < str(value or "")[:10]):
                return False
            if op == "after" and not (str(cell or "")[:10] > str(value or "")[:10]):
                return False
            if op in {">", "gt", ">=", "gte", "<", "lt", "<=", "lte"}:
                try:
                    left = float(cell or 0)
                    right = float(value or 0)
                except (TypeError, ValueError):
                    return False
                if op in {">", "gt"} and not left > right:
                    return False
                if op in {">=", "gte"} and not left >= right:
                    return False
                if op in {"<", "lt"} and not left < right:
                    return False
                if op in {"<=", "lte"} and not left <= right:
                    return False
        return True

    filtered = [
        dict(record)
        for record in records
        if isinstance(record, dict) and passes_filters(record)
    ]

    def metric_value(record: Dict[str, Any]) -> float:
        if metric.aggregation == "count" or not metric.field_id:
            return 1.0
        value = record.get(metric.field_id)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    if dimension_field_id:
        buckets: Dict[str, List[float]] = {}
        for record in filtered:
            raw_dimension = record.get(dimension_field_id)
            buckets.setdefault(_dimension_key(raw_dimension), []).append(
                metric_value(record)
            )

        rows: List[Dict[str, Any]] = []
        for dimension, values in buckets.items():
            if metric.aggregation == "sum":
                aggregate = sum(values)
            elif metric.aggregation == "avg":
                aggregate = sum(values) / len(values) if values else 0.0
            elif metric.aggregation == "max":
                aggregate = max(values) if values else 0.0
            elif metric.aggregation == "min":
                aggregate = min(values) if values else 0.0
            else:
                aggregate = float(len(values))
            rows.append({"dimension": dimension, "value": float(aggregate)})

        order = str(sort.get("order") or "desc").lower()
        order_by = str(sort.get("by") or "value").lower()
        if order_by == "dimension":
            rows.sort(
                key=lambda row: str(row.get("dimension") or ""),
                reverse=order != "asc",
            )
        else:
            rows.sort(
                key=lambda row: float(row.get("value") or 0),
                reverse=order != "asc",
            )
        return {
            "rows": rows[:limit_n],
            "dimensionFieldId": dimension_field_id,
            "metric": {
                "fieldId": metric.field_id,
                "aggregation": metric.aggregation,
            },
        }

    values = [metric_value(record) for record in filtered]
    if metric.aggregation == "sum":
        aggregate = sum(values)
    elif metric.aggregation == "avg":
        aggregate = sum(values) / len(values) if values else 0.0
    elif metric.aggregation == "max":
        aggregate = max(values) if values else 0.0
    elif metric.aggregation == "min":
        aggregate = min(values) if values else 0.0
    else:
        aggregate = float(len(values))

    return {
        "rows": [{"value": float(aggregate)}],
        "dimensionFieldId": None,
        "metric": {
            "fieldId": metric.field_id,
            "aggregation": metric.aggregation,
        },
    }


async def list_table_fields_db(db: AsyncSession, table_id: str) -> List[Dict[str, Any]]:
    result = await db.execute(
        select(TableField).where(TableField.table_id == table_id).order_by(TableField.order_index.asc())
    )
    fields = result.scalars().all()
    return [
        {
            "id": f.id,
            "name": f.name,
            "type": f.type,
            "options": f.options,
            "property": f.property,
        }
        for f in fields
    ]


def compute_widget_data_file(config: Dict[str, Any]) -> Dict[str, Any]:
    table_id = config.get("tableId")
    if not isinstance(table_id, str) or not table_id:
        return {"rows": [], "metric": {"aggregation": "count"}}

    from app.services.smart_table_store.helpers import _load_table_json

    store = _TABLE_JSON_CACHE.get(table_id)
    if store is None:
        loaded = _load_table_json(table_id)
        try:
            import os

            from app.services.smart_table_store.helpers import _table_file_path

            mtime = os.path.getmtime(_table_file_path(table_id))
        except Exception:
            mtime = 0.0
        _TABLE_JSON_CACHE[table_id] = (mtime, loaded)
        store = _TABLE_JSON_CACHE.get(table_id)
    else:
        stored_mtime, stored_payload = store
        try:
            import os

            from app.services.smart_table_store.helpers import _table_file_path

            mtime = os.path.getmtime(_table_file_path(table_id))
        except Exception:
            mtime = stored_mtime
        if mtime != stored_mtime:
            loaded = _load_table_json(table_id)
            _TABLE_JSON_CACHE[table_id] = (mtime, loaded)
            store = _TABLE_JSON_CACHE.get(table_id)

    store = store[1] if store else _load_table_json(table_id)
    records = store.get("records", [])
    if not isinstance(records, list):
        records = []

    dimension_field_id = config.get("dimensionFieldId")
    dimension_field_id = dimension_field_id if isinstance(dimension_field_id, str) else None
    metric = _normalize_metric(config)
    filters = config.get("filters")
    filters = filters if isinstance(filters, list) else []
    sort = config.get("sort")
    sort = sort if isinstance(sort, dict) else {}
    limit = config.get("limit")
    limit_n = int(limit) if isinstance(limit, int) or isinstance(limit, float) else 200
    limit_n = max(1, min(limit_n, 1000))

    def passes_filters(record: Dict[str, Any]) -> bool:
        for f in filters:
            if not isinstance(f, dict):
                continue
            fid = f.get("fieldId")
            op = f.get("operator")
            value = f.get("value")
            if not isinstance(fid, str) or not isinstance(op, str):
                continue
            cell = record.get(fid)
            op_l = op.lower()
            if op_l in {"=", "eq"} and not (cell == value):
                return False
            if op_l in {"!=", "neq"} and not (cell != value):
                return False
            if op_l in {"contains"} and (value is not None) and (str(value) not in str(cell or "")):
                return False
            if op_l in {"in"} and isinstance(value, list) and (cell not in value):
                return False
            if op_l == "before" and not (str(cell or "")[:10] < str(value or "")[:10]):
                return False
            if op_l == "after" and not (str(cell or "")[:10] > str(value or "")[:10]):
                return False
            if op_l in {">", "gt"} and not (float(cell or 0) > float(value or 0)):
                return False
            if op_l in {">=", "gte"} and not (float(cell or 0) >= float(value or 0)):
                return False
            if op_l in {"<", "lt"} and not (float(cell or 0) < float(value or 0)):
                return False
            if op_l in {"<=", "lte"} and not (float(cell or 0) <= float(value or 0)):
                return False
        return True

    filtered: List[Dict[str, Any]] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        if passes_filters(r):
            filtered.append(r)

    def metric_value(record: Dict[str, Any]) -> float:
        if metric.aggregation == "count" or not metric.field_id:
            return 1.0
        v = record.get(metric.field_id)
        try:
            return float(v)
        except Exception:
            return 0.0

    if dimension_field_id:
        buckets: Dict[str, List[float]] = {}
        for r in filtered:
            key = r.get(dimension_field_id)
            buckets.setdefault(_dimension_key(key), []).append(metric_value(r))

        rows: List[Dict[str, Any]] = []
        for k, values in buckets.items():
            if metric.aggregation == "sum":
                agg = sum(values)
            elif metric.aggregation == "avg":
                agg = sum(values) / len(values) if values else 0
            elif metric.aggregation == "max":
                agg = max(values) if values else 0
            elif metric.aggregation == "min":
                agg = min(values) if values else 0
            else:
                agg = float(len(values))
            rows.append({"dimension": k, "value": float(agg)})

        order = str(sort.get("order") or "desc").lower()
        order_by = str(sort.get("by") or "value").lower()
        if order_by == "dimension":
            rows.sort(key=lambda x: str(x.get("dimension") or ""), reverse=order != "asc")
        else:
            rows.sort(key=lambda x: float(x.get("value") or 0), reverse=order != "asc")
        rows = rows[:limit_n]
        return {
            "rows": rows,
            "dimensionFieldId": dimension_field_id,
            "metric": {"fieldId": metric.field_id, "aggregation": metric.aggregation},
        }

    values = [metric_value(r) for r in filtered]
    if metric.aggregation == "sum":
        agg = sum(values)
    elif metric.aggregation == "avg":
        agg = sum(values) / len(values) if values else 0
    elif metric.aggregation == "max":
        agg = max(values) if values else 0
    elif metric.aggregation == "min":
        agg = min(values) if values else 0
    else:
        agg = float(len(values))
    return {
        "rows": [{"value": float(agg)}],
        "dimensionFieldId": None,
        "metric": {"fieldId": metric.field_id, "aggregation": metric.aggregation},
    }
