from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dashboard import Dashboard, DashboardWidget
from .helpers import _generate_id, generate_public_token


async def ensure_dashboard_db(
    db: AsyncSession, dashboard_id: str, workspace_id: str
) -> Dashboard:
    result = await db.execute(select(Dashboard).where(Dashboard.id == dashboard_id))
    row = result.scalars().first()
    if row:
        return row
    row = Dashboard(id=dashboard_id, workspace_id=workspace_id, is_public=False)
    db.add(row)
    await db.commit()
    return row


async def get_dashboard_db(db: AsyncSession, dashboard_id: str) -> Optional[Dashboard]:
    result = await db.execute(select(Dashboard).where(Dashboard.id == dashboard_id))
    return result.scalars().first()


async def update_dashboard_meta_db(
    db: AsyncSession, dashboard_id: str, updates: Dict[str, Any]
) -> Optional[Dashboard]:
    row = await get_dashboard_db(db, dashboard_id)
    if not row:
        return None
    if "description" in updates:
        row.description = updates.get("description")
    if "isPublic" in updates:
        row.is_public = bool(updates.get("isPublic"))
    if "publicToken" in updates:
        row.public_token = updates.get("publicToken")
    await db.commit()
    return row


async def ensure_public_token_db(db: AsyncSession, dashboard_id: str) -> Optional[str]:
    row = await get_dashboard_db(db, dashboard_id)
    if not row:
        return None
    if row.public_token:
        return row.public_token
    row.public_token = generate_public_token()
    await db.commit()
    return row.public_token


async def list_widgets_db(db: AsyncSession, dashboard_id: str) -> List[DashboardWidget]:
    result = await db.execute(
        select(DashboardWidget)
        .where(DashboardWidget.dashboard_id == dashboard_id)
        .order_by(DashboardWidget.order_index.asc(), DashboardWidget.created_at.asc())
    )
    return list(result.scalars().all())


async def create_widget_db(
    db: AsyncSession, dashboard_id: str, widget: Dict[str, Any]
) -> DashboardWidget:
    result_max = await db.execute(
        select(func.max(DashboardWidget.order_index)).where(
            DashboardWidget.dashboard_id == dashboard_id
        )
    )
    max_order = result_max.scalar() or 0
    widget_id = _generate_id("wdg")
    row = DashboardWidget(
        id=widget_id,
        dashboard_id=dashboard_id,
        type=str(widget.get("type") or "bar"),
        title=widget.get("title") or "",
        color_scheme=widget.get("colorScheme"),
        layout=widget.get("layout") or {"x": 0, "y": 0, "w": 6, "h": 8},
        config=widget.get("config") or {},
        order_index=max_order + 1,
    )
    db.add(row)
    await db.commit()
    return row


async def update_widget_db(
    db: AsyncSession, widget_id: str, updates: Dict[str, Any]
) -> Optional[DashboardWidget]:
    result = await db.execute(select(DashboardWidget).where(DashboardWidget.id == widget_id))
    row = result.scalars().first()
    if not row:
        return None
    if "type" in updates:
        row.type = str(updates.get("type") or row.type)
    if "title" in updates:
        row.title = updates.get("title")
    if "colorScheme" in updates:
        row.color_scheme = updates.get("colorScheme")
    if "layout" in updates and isinstance(updates.get("layout"), dict):
        row.layout = updates.get("layout")
    if "config" in updates and isinstance(updates.get("config"), dict):
        row.config = updates.get("config")
    await db.commit()
    return row


async def delete_widget_db(db: AsyncSession, widget_id: str) -> bool:
    result = await db.execute(delete(DashboardWidget).where(DashboardWidget.id == widget_id))
    await db.commit()
    return (result.rowcount or 0) > 0


async def delete_dashboard_db(db: AsyncSession, dashboard_id: str) -> None:
    await db.execute(delete(DashboardWidget).where(DashboardWidget.dashboard_id == dashboard_id))
    await db.execute(delete(Dashboard).where(Dashboard.id == dashboard_id))
    await db.commit()


async def copy_dashboard_db(
    db: AsyncSession, source_dashboard_id: str, target_dashboard_id: str, workspace_id: str
) -> None:
    source = await get_dashboard_db(db, source_dashboard_id)
    if source:
        db.add(
            Dashboard(
                id=target_dashboard_id,
                workspace_id=workspace_id,
                description=source.description,
                is_public=False,
                public_token=None,
            )
        )
    else:
        db.add(
            Dashboard(
                id=target_dashboard_id,
                workspace_id=workspace_id,
                description="",
                is_public=False,
                public_token=None,
            )
        )
    await db.commit()
    widgets = await list_widgets_db(db, source_dashboard_id)
    for idx, w in enumerate(widgets):
        db.add(
            DashboardWidget(
                id=_generate_id("wdg"),
                dashboard_id=target_dashboard_id,
                type=w.type,
                title=w.title,
                color_scheme=w.color_scheme,
                layout=w.layout,
                config=w.config,
                order_index=idx + 1,
            )
        )
    await db.commit()
