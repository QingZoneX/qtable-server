"""Runtime-only Dashboard filters layered onto persisted widget configuration.

Runtime filters are intentionally not persisted on the Dashboard or widget. They are
validated against the widget source table on every request, then evaluated through
the existing permission-aware dashboard runtime.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dashboard import DashboardWidget
from app.services.dashboard_runtime import (
    DashboardValidationError,
    _field_map,
    _normalize_filters,
)
from app.services.dashboard_runtime_resilience import compute_widget_data_resilient


async def compute_widget_data_with_runtime_filters(
    db: AsyncSession,
    widget: DashboardWidget,
    *,
    user_id: int,
    runtime_filters: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compute widget data with validated, request-scoped filters.

    Persisted widget filters remain the base query. Runtime filters are appended with
    AND semantics and never mutate the ORM widget or its stored config. Execution uses
    the normal SQL runtime first and falls back to the already-authorized record path
    only for malformed legacy values that cannot be converted by the database.
    """
    if not runtime_filters:
        return await compute_widget_data_resilient(db, widget, user_id=user_id)

    config = dict(widget.config) if isinstance(widget.config, dict) else {}
    table_id = str(config.get("tableId") or "")
    if not table_id:
        raise DashboardValidationError(
            "Runtime filters require a configured dashboard source table"
        )

    fields = await _field_map(db, table_id)
    normalized_runtime_filters = _normalize_filters(runtime_filters, fields)
    persisted_filters = config.get("filters")
    persisted_filters = persisted_filters if isinstance(persisted_filters, list) else []
    config["filters"] = [*persisted_filters, *normalized_runtime_filters]

    runtime_widget = SimpleNamespace(
        id=widget.id,
        dashboard_id=widget.dashboard_id,
        config=config,
    )
    return await compute_widget_data_resilient(
        db,
        runtime_widget,  # type: ignore[arg-type]
        user_id=user_id,
    )
