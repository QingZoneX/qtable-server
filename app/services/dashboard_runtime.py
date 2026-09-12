"""Dashboard validation, secure data execution, and public sharing helpers."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dashboard import Dashboard, DashboardWidget
from app.models.smart_table import TableField, WorkspaceItem
from app.services.dashboard_analytics import compute_widget_data_db, compute_widget_data_records
from app.services.dashboard_cache import DashboardCacheTTL, get_dashboard_cache
from app.services.row_permissions import (
    allowed_record_ids,
    filter_store_for_user,
    get_row_permission_policy,
    row_permission_restricts_user,
)
from app.services.smart_table_store import get_full_store
from app.services.workspace import get_effective_permission_for_item, permission_allows


DASHBOARD_WIDGET_TYPES = {
    "bar", "line", "pie", "horizontalBar", "table", "metric", "progress"
}
DIMENSION_WIDGET_TYPES = {"bar", "line", "pie", "horizontalBar", "table"}
AGGREGATIONS = {"count", "sum", "avg", "max", "min"}
FILTER_OPERATORS = {"eq", "neq", "contains", "gt", "gte", "lt", "lte", "in", "before", "after"}
NUMERIC_FIELD_TYPES = {"number", "progress", "rating", "autoNumber"}
NUMERIC_FILTER_OPERATORS = {"gt", "gte", "lt", "lte"}
DATE_FILTER_OPERATORS = {"before", "after"}
MAX_FILTERS = 50
MAX_WIDGET_LIMIT = 1000


class DashboardValidationError(ValueError):
    """Raised when a dashboard widget configuration is invalid."""


async def _dashboard_row(db: AsyncSession, dashboard_id: str) -> Dashboard:
    result = await db.execute(select(Dashboard).where(Dashboard.id == dashboard_id).limit(1))
    dashboard = result.scalars().first()
    if dashboard is None:
        raise DashboardValidationError("Dashboard not found")
    return dashboard


async def _source_table(
    db: AsyncSession,
    table_id: str,
    *,
    workspace_id: str,
) -> WorkspaceItem:
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == table_id,
            WorkspaceItem.type == "table",
        )
    )
    item = result.scalars().first()
    if item is None:
        raise DashboardValidationError("Source table not found")
    if item.workspace_id != workspace_id:
        raise DashboardValidationError(
            "Dashboard widgets cannot use a table from another workspace"
        )
    return item


async def _field_map(db: AsyncSession, table_id: str) -> Dict[str, TableField]:
    result = await db.execute(select(TableField).where(TableField.table_id == table_id))
    return {str(field.id): field for field in result.scalars().all()}


def _normalize_layout(value: Any) -> Dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    try:
        x = int(raw.get("x", 0))
        y = int(raw.get("y", 0))
        w = int(raw.get("w", 6))
        h = int(raw.get("h", 8))
    except (TypeError, ValueError) as exc:
        raise DashboardValidationError("Widget layout must contain numeric values") from exc
    if w < 1 or w > 12 or h < 2 or h > 100:
        raise DashboardValidationError("Widget layout size is out of range")
    if x < 0 or x > 11 or y < 0:
        raise DashboardValidationError("Widget layout position is out of range")
    if x + w > 12:
        x = max(0, 12 - w)
    return {"x": x, "y": min(y, 999), "w": w, "h": h}


def _normalize_title(value: Any) -> str:
    title = str(value or "").strip()
    if len(title) > 255:
        raise DashboardValidationError("Widget title is too long")
    return title


def _normalize_filters(
    value: Any,
    fields: Dict[str, TableField],
) -> list[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DashboardValidationError("Widget filters must be a list")
    if len(value) > MAX_FILTERS:
        raise DashboardValidationError(
            f"Dashboard widgets support at most {MAX_FILTERS} filters"
        )
    filters: list[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise DashboardValidationError("Every widget filter must be an object")
        field_id = str(item.get("fieldId") or "")
        field = fields.get(field_id)
        if field is None:
            raise DashboardValidationError(
                f"Widget filter references missing field: {field_id or '<empty>'}"
            )
        operator = str(item.get("operator") or "").lower()
        if operator not in FILTER_OPERATORS:
            raise DashboardValidationError(
                f"Unsupported dashboard filter operator: {operator or '<empty>'}"
            )
        if operator in NUMERIC_FILTER_OPERATORS and field.type not in NUMERIC_FIELD_TYPES:
            raise DashboardValidationError(f"Operator {operator} requires a numeric field")
        if operator in DATE_FILTER_OPERATORS and field.type != "date":
            raise DashboardValidationError(f"Operator {operator} requires a date field")
        filter_value = item.get("value")
        if operator == "in" and not isinstance(filter_value, list):
            raise DashboardValidationError("The in operator requires a list value")
        if operator in DATE_FILTER_OPERATORS:
            try:
                filter_value = date.fromisoformat(str(filter_value or "")[:10]).isoformat()
            except ValueError as exc:
                raise DashboardValidationError(
                    f"Operator {operator} requires a YYYY-MM-DD date value"
                ) from exc
        filters.append(
            {"fieldId": field_id, "operator": operator, "value": filter_value}
        )
    return filters


def public_render_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Expose only presentation settings, never source/query internals."""
    payload: Dict[str, Any] = {}
    target = config.get("targetValue")
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        payload["targetValue"] = float(target)
    display = config.get("display")
    if isinstance(display, dict):
        allowed = {"showLegend", "showLabels", "decimals", "prefix", "suffix"}
        payload["display"] = {key: display[key] for key in allowed if key in display}
    return payload


async def validate_widget_candidate(
    db: AsyncSession,
    dashboard_id: str,
    payload: Dict[str, Any],
    *,
    existing: Optional[DashboardWidget] = None,
    require_complete: bool = True,
) -> Dict[str, Any]:
    """Validate and normalize a widget before it is persisted."""
    dashboard = await _dashboard_row(db, dashboard_id)
    widget_type = str(
        payload.get("type")
        if "type" in payload
        else (existing.type if existing is not None else "bar")
    )
    if widget_type not in DASHBOARD_WIDGET_TYPES:
        raise DashboardValidationError(f"Unsupported widget type: {widget_type}")
    title = _normalize_title(
        payload.get("title")
        if "title" in payload
        else (existing.title if existing is not None else "")
    )
    layout = _normalize_layout(
        payload.get("layout")
        if "layout" in payload
        else (existing.layout if existing is not None else None)
    )
    color_scheme = (
        payload.get("colorScheme")
        if "colorScheme" in payload
        else (existing.color_scheme if existing is not None else None)
    )
    raw_config = (
        payload.get("config")
        if "config" in payload
        else (existing.config if existing is not None else {})
    )
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise DashboardValidationError("Widget config must be an object")

    table_id_raw = raw_config.get("tableId")
    table_id = str(table_id_raw).strip() if isinstance(table_id_raw, str) else ""
    if not table_id:
        if require_complete:
            raise DashboardValidationError("Select a source table before saving")
        return {
            "type": widget_type,
            "title": title,
            "colorScheme": color_scheme,
            "layout": layout,
            "config": {
                "tableId": None,
                "dimensionFieldId": None,
                "metric": {"aggregation": "count", "fieldId": None},
                "filters": [],
                "sort": {"by": "value", "order": "desc"},
                "limit": 50,
            },
        }

    await _source_table(db, table_id, workspace_id=dashboard.workspace_id)
    fields = await _field_map(db, table_id)

    dimension_raw = raw_config.get("dimensionFieldId")
    dimension_id = str(dimension_raw).strip() if isinstance(dimension_raw, str) else ""
    if dimension_id and dimension_id not in fields:
        raise DashboardValidationError(f"Dimension references missing field: {dimension_id}")
    if widget_type in DIMENSION_WIDGET_TYPES and require_complete and not dimension_id:
        raise DashboardValidationError("This widget type requires a dimension field")
    if widget_type not in DIMENSION_WIDGET_TYPES:
        dimension_id = ""

    metric_raw = raw_config.get("metric")
    metric_raw = metric_raw if isinstance(metric_raw, dict) else {}
    aggregation = str(metric_raw.get("aggregation") or "count").lower()
    if aggregation not in AGGREGATIONS:
        raise DashboardValidationError(f"Unsupported aggregation: {aggregation}")
    metric_field_raw = metric_raw.get("fieldId")
    metric_field_id = (
        str(metric_field_raw).strip() if isinstance(metric_field_raw, str) else ""
    )
    if metric_field_id and metric_field_id not in fields:
        raise DashboardValidationError(f"Metric references missing field: {metric_field_id}")
    if aggregation != "count":
        if not metric_field_id:
            if require_complete:
                raise DashboardValidationError(
                    f"{aggregation} aggregation requires a numeric field"
                )
        elif fields[metric_field_id].type not in NUMERIC_FIELD_TYPES:
            raise DashboardValidationError(
                f"{aggregation} aggregation requires a numeric field"
            )
    else:
        metric_field_id = ""

    filters = _normalize_filters(raw_config.get("filters"), fields)
    sort_raw = raw_config.get("sort")
    sort_raw = sort_raw if isinstance(sort_raw, dict) else {}
    sort_by = str(sort_raw.get("by") or "value").lower()
    sort_order = str(sort_raw.get("order") or "desc").lower()
    if sort_by not in {"value", "dimension"}:
        raise DashboardValidationError("Widget sort must use value or dimension")
    if sort_order not in {"asc", "desc"}:
        raise DashboardValidationError("Widget sort order must be asc or desc")
    if not dimension_id and sort_by == "dimension":
        sort_by = "value"

    raw_limit = raw_config.get("limit", 50)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise DashboardValidationError("Widget limit must be a number") from exc
    if limit < 1 or limit > MAX_WIDGET_LIMIT:
        raise DashboardValidationError(
            f"Widget limit must be between 1 and {MAX_WIDGET_LIMIT}"
        )

    normalized_config: Dict[str, Any] = {
        "tableId": table_id,
        "dimensionFieldId": dimension_id or None,
        "metric": {"aggregation": aggregation, "fieldId": metric_field_id or None},
        "filters": filters,
        "sort": {"by": sort_by, "order": sort_order},
        "limit": limit,
    }

    if widget_type == "progress":
        target = raw_config.get("targetValue")
        try:
            target_value = float(target)
        except (TypeError, ValueError):
            target_value = 0.0
        if require_complete and target_value <= 0:
            raise DashboardValidationError(
                "Progress widgets require a target value greater than zero"
            )
        if target_value > 0:
            normalized_config["targetValue"] = target_value

    display = raw_config.get("display")
    if isinstance(display, dict):
        normalized_config["display"] = dict(display)

    return {
        "type": widget_type,
        "title": title,
        "colorScheme": color_scheme,
        "layout": layout,
        "config": normalized_config,
    }


def widget_source_table_id(widget_payload: Dict[str, Any]) -> Optional[str]:
    config = widget_payload.get("config")
    if not isinstance(config, dict):
        return None
    table_id = config.get("tableId")
    return table_id if isinstance(table_id, str) and table_id else None


async def compute_widget_data_for_user(
    db: AsyncSession,
    widget: DashboardWidget,
    *,
    user_id: int,
    table_permission: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute widget data using one user's current table/row visibility."""
    config = widget.config if isinstance(widget.config, dict) else {}
    table_id = str(config.get("tableId") or "")
    if not table_id:
        return {"rows": [], "metric": {"aggregation": "count"}}

    dashboard = await _dashboard_row(db, widget.dashboard_id)
    await _source_table(
        db,
        table_id,
        workspace_id=dashboard.workspace_id,
    )
    permission = table_permission or await get_effective_permission_for_item(
        db, user_id, table_id
    )
    if not permission_allows(permission, "read"):
        raise PermissionError("Dashboard source table is not available")

    policy = await get_row_permission_policy(db, table_id)
    restricted = row_permission_restricts_user(policy, permission)
    visible_ids: Optional[list[str]] = None
    if restricted:
        visible_ids = list(
            await allowed_record_ids(
                db,
                table_id,
                user_id=user_id,
                table_permission=permission,
                policy=policy,
            )
        )

    fields_result = await db.execute(
        select(TableField.id, TableField.type).where(TableField.table_id == table_id)
    )
    relation_field_ids = {
        str(field_id)
        for field_id, field_type in fields_result.all()
        if str(field_type or "").lower() == "relation"
    }
    metric_config = config.get("metric") if isinstance(config.get("metric"), dict) else {}
    used_field_ids = {
        str(value)
        for value in (config.get("dimensionFieldId"), metric_config.get("fieldId"))
        if isinstance(value, str) and value
    }
    for filter_item in config.get("filters") or []:
        if isinstance(filter_item, dict):
            field_id = filter_item.get("fieldId")
            if isinstance(field_id, str) and field_id:
                used_field_ids.add(field_id)
    relation_sensitive = bool(relation_field_ids.intersection(used_field_ids))
    user_specific_scope = restricted or relation_sensitive

    cache = get_dashboard_cache()
    cache_key = cache.widget_key(table_id, widget.id, {"config": config})
    if not user_specific_scope:
        cached = await cache.get(cache_key)
        if cached:
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    if relation_sensitive:
        store = await get_full_store(db, table_id)
        visible_store, _ = await filter_store_for_user(
            db,
            table_id,
            store,
            user_id=user_id,
            table_permission=permission,
        )
        computed = compute_widget_data_records(
            config, list(visible_store.get("records", []))
        )
    else:
        computed = await compute_widget_data_db(
            db, config, allowed_record_ids=visible_ids
        )

    if not user_specific_scope:
        await cache.set(cache_key, computed, ttl=DashboardCacheTTL.WIDGET_DATA)
    return computed


async def set_dashboard_publication_actor(
    db: AsyncSession,
    dashboard_id: str,
    *,
    is_public: bool,
    user_id: int,
) -> Dashboard:
    dashboard = await _dashboard_row(db, dashboard_id)
    dashboard.published_by_user_id = user_id if is_public else None
    await db.commit()
    await db.refresh(dashboard)
    return dashboard


async def ensure_publication_actor(
    db: AsyncSession,
    dashboard_id: str,
    *,
    user_id: int,
) -> Dashboard:
    dashboard = await _dashboard_row(db, dashboard_id)
    if dashboard.is_public and dashboard.published_by_user_id is None:
        dashboard.published_by_user_id = user_id
        await db.commit()
        await db.refresh(dashboard)
    return dashboard


async def _public_dashboard_by_token(
    db: AsyncSession, token: str
) -> tuple[Dashboard, WorkspaceItem]:
    result = await db.execute(
        select(Dashboard).where(
            Dashboard.public_token == token,
            Dashboard.is_public.is_(True),
        )
    )
    dashboard = result.scalars().first()
    if dashboard is None or dashboard.published_by_user_id is None:
        raise PermissionError("Public dashboard is unavailable")
    item_result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == dashboard.id,
            WorkspaceItem.type == "dashboard",
        )
    )
    item = item_result.scalars().first()
    if item is None:
        raise PermissionError("Public dashboard is unavailable")
    return dashboard, item


async def public_dashboard_payload(
    db: AsyncSession, token: str
) -> Dict[str, Any]:
    dashboard, item = await _public_dashboard_by_token(db, token)
    result = await db.execute(
        select(DashboardWidget)
        .where(DashboardWidget.dashboard_id == dashboard.id)
        .order_by(DashboardWidget.order_index.asc(), DashboardWidget.created_at.asc())
    )
    widgets = []
    for widget in result.scalars().all():
        try:
            await validate_widget_candidate(
                db,
                dashboard.id,
                {},
                existing=widget,
                require_complete=True,
            )
        except DashboardValidationError:
            # Managers may be configuring a draft widget while the dashboard
            # stays public. Drafts remain invisible until they are valid.
            continue
        config = widget.config if isinstance(widget.config, dict) else {}
        widgets.append(
            {
                "id": widget.id,
                "type": widget.type,
                "title": widget.title or "",
                "colorScheme": widget.color_scheme,
                "layout": widget.layout,
                "config": public_render_config(config),
            }
        )
    return {
        "id": dashboard.id,
        "name": item.name,
        "description": dashboard.description or "",
        "isPublic": True,
        "widgets": widgets,
    }


async def public_widget_data(
    db: AsyncSession,
    token: str,
    widget_id: str,
) -> Dict[str, Any]:
    dashboard, _ = await _public_dashboard_by_token(db, token)
    widget_result = await db.execute(
        select(DashboardWidget).where(
            DashboardWidget.id == widget_id,
            DashboardWidget.dashboard_id == dashboard.id,
        )
    )
    widget = widget_result.scalars().first()
    if widget is None:
        raise PermissionError("Public dashboard widget is unavailable")
    try:
        await validate_widget_candidate(
            db,
            dashboard.id,
            {},
            existing=widget,
            require_complete=True,
        )
    except DashboardValidationError as exc:
        raise PermissionError(
            "Public dashboard widget is unavailable"
        ) from exc

    config = widget.config if isinstance(widget.config, dict) else {}
    table_id = str(config.get("tableId") or "")
    if not table_id:
        return {"rows": [], "metric": {"aggregation": "count"}}
    await _source_table(db, table_id, workspace_id=dashboard.workspace_id)

    publisher_id = int(dashboard.published_by_user_id)
    permission = await get_effective_permission_for_item(db, publisher_id, table_id)
    if not permission_allows(permission, "read"):
        raise PermissionError("Public dashboard source is unavailable")
    return await compute_widget_data_for_user(
        db,
        widget,
        user_id=publisher_id,
        table_permission=permission,
    )
