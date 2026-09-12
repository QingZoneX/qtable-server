"""Permission-safe fallback for Dashboard aggregation data conversion failures."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.exc import DBAPIError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dashboard import DashboardWidget
from app.services.dashboard_analytics import compute_widget_data_records
from app.services.dashboard_runtime import (
    DashboardValidationError,
    _dashboard_row,
    _source_table,
    compute_widget_data_for_user,
)
from app.services.row_permissions import filter_store_for_user
from app.services.smart_table_store import get_full_store
from app.services.workspace import get_effective_permission_for_item, permission_allows


def _is_data_conversion_failure(exc: BaseException) -> bool:
    """Keep validation/access failures explicit; only recover execution/data errors."""
    if isinstance(exc, (DashboardValidationError, PermissionError)):
        return False
    return isinstance(exc, (DBAPIError, StatementError, TypeError, ValueError))


async def _compute_from_authorized_records(
    db: AsyncSession,
    widget: DashboardWidget,
    *,
    user_id: int,
) -> Dict[str, Any]:
    config = widget.config if isinstance(widget.config, dict) else {}
    table_id = str(config.get("tableId") or "")
    if not table_id:
        return {"rows": [], "metric": {"aggregation": "count"}}

    dashboard = await _dashboard_row(db, widget.dashboard_id)
    await _source_table(db, table_id, workspace_id=dashboard.workspace_id)
    permission = await get_effective_permission_for_item(db, user_id, table_id)
    if not permission_allows(permission, "read"):
        raise PermissionError("Dashboard source table is not available")

    store = await get_full_store(db, table_id)
    visible_store, _ = await filter_store_for_user(
        db,
        table_id,
        store,
        user_id=user_id,
        table_permission=permission,
    )
    records = visible_store.get("records", [])
    safe_records = [record for record in records if isinstance(record, dict)]
    return compute_widget_data_records(config, safe_records)


async def compute_widget_data_resilient(
    db: AsyncSession,
    widget: DashboardWidget,
    *,
    user_id: int,
) -> Dict[str, Any]:
    """Use SQL fast-path, then authorized in-memory aggregation for bad legacy values.

    PostgreSQL statement failures abort the active transaction, so the fast-path is
    isolated in a savepoint. Missing sources, invalid widget configuration and access
    loss are deliberately not downgraded to fallback results.
    """
    try:
        async with db.begin_nested():
            return await compute_widget_data_for_user(db, widget, user_id=user_id)
    except Exception as exc:
        if not _is_data_conversion_failure(exc):
            raise
        return await _compute_from_authorized_records(db, widget, user_id=user_id)
