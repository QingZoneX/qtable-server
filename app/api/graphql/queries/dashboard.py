from __future__ import annotations

import os
import time
import logging
from typing import Optional

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _require_item_permission,
    _require_user,
    _resolve_backend,
)
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.smart_table import TableField, WorkspaceItem
from app.services.dashboard_analytics import compute_widget_data_file
from app.services.dashboard_runtime import (
    DashboardValidationError,
    public_dashboard_payload,
    public_widget_data,
)
from app.services.dashboard_runtime_filters import compute_widget_data_with_runtime_filters
from app.services.dashboard_cache import DashboardCacheTTL, get_dashboard_cache
from app.services.dashboard_store.file_backend import get_dashboard_file, list_widgets_file
from app.services.dashboard_store.helpers import DASHBOARDS_DIR

logger = logging.getLogger(__name__)

@strawberry.type
class DashboardQueries:
    @strawberry.field
    async def dashboard(
        self,
        info: Info,
        dashboard_id: str,
        workspace_id: Optional[str] = None,
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        await _require_item_permission(info, dashboard_id, "read")

        if _resolve_backend(None) == "db":
            result_item = await db.execute(
                select(WorkspaceItem).where(WorkspaceItem.id == dashboard_id)
            )
            item = result_item.scalars().first()
            if not item or item.type != "dashboard":
                raise ValueError("Dashboard not found")
            result_dash = await db.execute(select(Dashboard).where(Dashboard.id == dashboard_id))
            dash = result_dash.scalars().first()
            meta = {
                "id": dashboard_id,
                "name": item.name,
                "description": (dash.description if dash else "") or "",
                "isPublic": bool(dash.is_public) if dash else False,
                "publicToken": dash.public_token if dash else None,
            }
            widgets_result = await db.execute(
                select(DashboardWidget).where(DashboardWidget.dashboard_id == dashboard_id)
            )
            widgets = [
                {
                    "id": w.id,
                    "type": w.type,
                    "title": w.title or "",
                    "colorScheme": w.color_scheme,
                    "layout": w.layout,
                    "config": w.config or {},
                }
                for w in widgets_result.scalars().all()
            ]
            meta["widgets"] = widgets
            return meta

        payload = get_dashboard_file(dashboard_id)
        payload = dict(payload)
        payload.setdefault("id", dashboard_id)
        payload.setdefault("name", dashboard_id)
        payload["widgets"] = list_widgets_file(dashboard_id)
        return payload

    @strawberry.field
    async def dashboardWidgetData(
        self,
        info: Info,
        widget_id: str,
        dashboard_id: Optional[str] = None,
        runtime_filters: Optional[JSON] = None,
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            result = await db.execute(
                select(DashboardWidget).where(DashboardWidget.id == widget_id)
            )
            widget = result.scalars().first()
            if not widget:
                raise ValueError("Widget not found")
            await _require_item_permission(info, widget.dashboard_id, "read")
            user = await _require_user(info)
            if runtime_filters is not None and not isinstance(runtime_filters, list):
                raise ValueError("Dashboard runtime filters must be a list")
            try:
                return await compute_widget_data_with_runtime_filters(
                    db,
                    widget,
                    user_id=user.id,
                    runtime_filters=runtime_filters,
                )
            except (PermissionError, DashboardValidationError) as exc:
                if isinstance(exc, PermissionError):
                    raise ValueError(
                        "Dashboard source table is not available"
                    ) from exc
                raise ValueError(str(exc)) from exc

        cache = get_dashboard_cache()
        if not os.path.isdir(DASHBOARDS_DIR):
            return {"rows": [], "metric": {"aggregation": "count"}}

        payload = None
        if isinstance(dashboard_id, str) and dashboard_id:
            try:
                payload = get_dashboard_file(dashboard_id)
            except Exception:
                payload = None
        if isinstance(payload, dict):
            widgets = payload.get("widgets", [])
            if isinstance(widgets, list):
                for widget in widgets:
                    if isinstance(widget, dict) and widget.get("id") == widget_id:
                        config = widget.get("config") or {}
                        if runtime_filters:
                            raise ValueError(
                                "Dashboard runtime filters require the database backend"
                            )
                        table_id = str(config.get("tableId") or "")
                        cache_key = cache.widget_key(
                            table_id,
                            widget_id,
                            {"config": config},
                        )
                        cached = await cache.get(cache_key)
                        if cached:
                            return {
                                key: value
                                for key, value in cached.items()
                                if not key.startswith("_")
                            }
                        computed = compute_widget_data_file(config)
                        await cache.set(
                            cache_key,
                            computed,
                            ttl=DashboardCacheTTL.WIDGET_DATA,
                        )
                        return computed
            return {"rows": [], "metric": {"aggregation": "count"}}

        for file_name in os.listdir(DASHBOARDS_DIR):
            if not file_name.endswith(".json"):
                continue
            scanned_dashboard_id = file_name[:-5]
            payload = get_dashboard_file(scanned_dashboard_id)
            widgets = payload.get("widgets", [])
            if not isinstance(widgets, list):
                continue
            for widget in widgets:
                if isinstance(widget, dict) and widget.get("id") == widget_id:
                    if runtime_filters:
                        raise ValueError(
                            "Dashboard runtime filters require the database backend"
                        )
                    return compute_widget_data_file(widget.get("config") or {})
        return {"rows": [], "metric": {"aggregation": "count"}}

    @strawberry.field
    async def dashboardPublicWidgetData(
        self,
        info: Info,
        token: str,
        widget_id: str,
    ) -> JSON:
        """Anonymous-safe data query scoped to one public dashboard token."""
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            try:
                return await public_widget_data(db, token, widget_id)
            except PermissionError as exc:
                raise ValueError(
                    "Public dashboard widget is unavailable"
                ) from exc

        if not os.path.isdir(DASHBOARDS_DIR):
            raise ValueError("Public dashboard widget is unavailable")
        for file_name in os.listdir(DASHBOARDS_DIR):
            if not file_name.endswith(".json"):
                continue
            scanned_dashboard_id = file_name[:-5]
            payload = get_dashboard_file(scanned_dashboard_id)
            if not (
                payload.get("isPublic")
                and payload.get("publicToken") == token
            ):
                continue
            for widget in payload.get("widgets") or []:
                if isinstance(widget, dict) and widget.get("id") == widget_id:
                    return compute_widget_data_file(widget.get("config") or {})
        raise ValueError("Public dashboard widget is unavailable")

    @strawberry.field
    async def dashboardPublic(self, info: Info, token: str) -> JSON:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            try:
                return await public_dashboard_payload(db, token)
            except PermissionError as exc:
                raise ValueError("Dashboard not found") from exc

        if not os.path.isdir(DASHBOARDS_DIR):
            raise ValueError("Dashboard not found")
        for file_name in os.listdir(DASHBOARDS_DIR):
            if not file_name.endswith(".json"):
                continue
            dashboard_id = file_name[:-5]
            payload = get_dashboard_file(dashboard_id)
            if payload.get("isPublic") and payload.get("publicToken") == token:
                safe_widgets = []
                for widget in payload.get("widgets") or []:
                    if not isinstance(widget, dict):
                        continue
                    config = widget.get("config")
                    target = (
                        config.get("targetValue")
                        if isinstance(config, dict)
                        else None
                    )
                    safe_config = (
                        {"targetValue": target}
                        if isinstance(target, (int, float))
                        else {}
                    )
                    safe_widgets.append(
                        {
                            "id": widget.get("id"),
                            "type": widget.get("type"),
                            "title": widget.get("title") or "",
                            "colorScheme": widget.get("colorScheme"),
                            "layout": widget.get("layout"),
                            "config": safe_config,
                        }
                    )
                return {
                    "id": dashboard_id,
                    "name": payload.get("name") or dashboard_id,
                    "description": payload.get("description") or "",
                    "isPublic": True,
                    "widgets": safe_widgets,
                }
        raise ValueError("Dashboard not found")
