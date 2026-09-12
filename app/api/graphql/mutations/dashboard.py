from __future__ import annotations

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.api.graphql.helpers import _require_item_permission, _require_user, _resolve_backend
from app.models.dashboard import Dashboard, DashboardWidget
from app.services.dashboard_cache import get_dashboard_cache
from app.services.dashboard_runtime import (
    DashboardValidationError,
    ensure_publication_actor,
    set_dashboard_publication_actor,
    validate_widget_candidate,
    widget_source_table_id,
)
from app.services.dashboard_store import (
    create_widget,
    delete_widget,
    ensure_public_token,
    update_dashboard_meta,
    update_widget,
)


@strawberry.type
class DashboardMutations:
    @strawberry.mutation
    async def update_dashboard_meta(
        self, info: Info, dashboard_id: str, updates: JSON
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        await _require_item_permission(info, dashboard_id, "manage")
        user = (
            await _require_user(info)
            if _resolve_backend(None) == "db"
            else None
        )
        next_updates = dict(updates or {})

        if bool(next_updates.get("isPublic")):
            widgets_result = await db.execute(
                select(DashboardWidget).where(
                    DashboardWidget.dashboard_id == dashboard_id
                )
            )
            for widget in widgets_result.scalars().all():
                try:
                    normalized = await validate_widget_candidate(
                        db,
                        dashboard_id,
                        {},
                        existing=widget,
                        require_complete=True,
                    )
                except DashboardValidationError as exc:
                    raise ValueError(
                        f"Cannot publish dashboard: {exc}"
                    ) from exc
                source_table_id = widget_source_table_id(normalized)
                if source_table_id:
                    await _require_item_permission(
                        info,
                        source_table_id,
                        "read",
                    )

        payload = await update_dashboard_meta(
            db,
            dashboard_id,
            next_updates,
        )
        if payload is None:
            raise ValueError("Dashboard not found")
        if "isPublic" in next_updates and _resolve_backend(None) == "db":
            await set_dashboard_publication_actor(
                db,
                dashboard_id,
                is_public=bool(next_updates.get("isPublic")),
                user_id=user.id,
            )
        cache = get_dashboard_cache()
        await cache.delete_pattern(f"{cache.KEY_PREFIX}:table:*:widget:*")
        return payload

    @strawberry.mutation
    async def ensure_dashboard_public_token(self, info: Info, dashboard_id: str) -> str:
        db: AsyncSession = info.context["db"]
        await _require_item_permission(info, dashboard_id, "manage")
        user = (
            await _require_user(info)
            if _resolve_backend(None) == "db"
            else None
        )
        token = await ensure_public_token(db, dashboard_id)
        if not token:
            raise ValueError("Dashboard not found")
        if _resolve_backend(None) == "db":
            await ensure_publication_actor(
                db,
                dashboard_id,
                user_id=user.id,
            )
        return token

    @strawberry.mutation
    async def create_dashboard_widget(
        self, info: Info, dashboard_id: str, widget: JSON
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        required_permission = "edit"
        if _resolve_backend(None) == "db":
            dashboard_result = await db.execute(
                select(Dashboard).where(Dashboard.id == dashboard_id)
            )
            dashboard = dashboard_result.scalars().first()
            if dashboard and dashboard.is_public:
                required_permission = "manage"
        await _require_item_permission(
            info,
            dashboard_id,
            required_permission,
        )

        candidate = dict(widget or {})
        if _resolve_backend(None) == "db":
            try:
                candidate = await validate_widget_candidate(
                    db,
                    dashboard_id,
                    candidate,
                    require_complete=False,
                )
            except DashboardValidationError as exc:
                raise ValueError(str(exc)) from exc
            source_table_id = widget_source_table_id(candidate)
            if source_table_id:
                await _require_item_permission(
                    info,
                    source_table_id,
                    "read",
                )
        created = await create_widget(db, dashboard_id, candidate)
        cache = get_dashboard_cache()
        await cache.delete_pattern(f"{cache.KEY_PREFIX}:table:*:widget:*")
        return created

    @strawberry.mutation
    async def update_dashboard_widget(
        self, info: Info, widget_id: str, updates: JSON
    ) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            result = await db.execute(select(DashboardWidget).where(DashboardWidget.id == widget_id))
            widget = result.scalars().first()
            if not widget:
                raise ValueError("Widget not found")
            dashboard_result = await db.execute(
                select(Dashboard).where(
                    Dashboard.id == widget.dashboard_id
                )
            )
            dashboard = dashboard_result.scalars().first()
            required_permission = (
                "manage"
                if dashboard and dashboard.is_public
                else "edit"
            )
            await _require_item_permission(
                info,
                widget.dashboard_id,
                required_permission,
            )
            next_updates = dict(updates or {})
            strict = "config" in next_updates or "type" in next_updates
            try:
                candidate = await validate_widget_candidate(
                    db,
                    widget.dashboard_id,
                    next_updates,
                    existing=widget,
                    require_complete=strict,
                )
            except DashboardValidationError as exc:
                raise ValueError(str(exc)) from exc
            source_table_id = widget_source_table_id(candidate)
            if source_table_id:
                await _require_item_permission(
                    info,
                    source_table_id,
                    "read",
                )
            next_updates = {
                key: candidate[key]
                for key in (
                    "type",
                    "title",
                    "colorScheme",
                    "layout",
                    "config",
                )
            }
        else:
            next_updates = dict(updates or {})
        updated = await update_widget(db, widget_id, next_updates)
        if not updated:
            raise ValueError("Widget not found")
        cache = get_dashboard_cache()
        await cache.invalidate_widget(widget_id)
        return updated

    @strawberry.mutation
    async def delete_dashboard_widget(self, info: Info, widget_id: str) -> bool:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            result = await db.execute(select(DashboardWidget).where(DashboardWidget.id == widget_id))
            widget = result.scalars().first()
            if not widget:
                return False
            dashboard_result = await db.execute(
                select(Dashboard).where(
                    Dashboard.id == widget.dashboard_id
                )
            )
            dashboard = dashboard_result.scalars().first()
            required_permission = (
                "manage"
                if dashboard and dashboard.is_public
                else "edit"
            )
            await _require_item_permission(
                info,
                widget.dashboard_id,
                required_permission,
            )
        deleted = await delete_widget(db, widget_id)
        cache = get_dashboard_cache()
        await cache.invalidate_widget(widget_id)
        return deleted
