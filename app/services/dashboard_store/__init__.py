from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.api.graphql.helpers import _resolve_backend

from .file_backend import (
    copy_dashboard_file,
    create_widget_file,
    delete_dashboard_file,
    delete_widget_file,
    ensure_public_token_file,
    get_dashboard_file,
    list_widgets_file,
    update_dashboard_meta_file,
    update_widget_file,
)
from .db_backend import (
    copy_dashboard_db,
    create_widget_db,
    delete_dashboard_db,
    delete_widget_db,
    ensure_dashboard_db,
    ensure_public_token_db,
    get_dashboard_db,
    list_widgets_db,
    update_dashboard_meta_db,
    update_widget_db,
)


async def ensure_dashboard(
    db, dashboard_id: str, workspace_id: str
) -> Dict[str, Any]:
    if _resolve_backend(None) == "db":
        row = await ensure_dashboard_db(db, dashboard_id, workspace_id)
        return {
            "id": row.id,
            "workspaceId": row.workspace_id,
            "description": row.description or "",
            "isPublic": bool(row.is_public),
            "publicToken": row.public_token,
        }
    return get_dashboard_file(dashboard_id)


async def get_dashboard(db, dashboard_id: str) -> Optional[Dict[str, Any]]:
    if _resolve_backend(None) == "db":
        row = await get_dashboard_db(db, dashboard_id)
        if not row:
            return None
        return {
            "id": row.id,
            "workspaceId": row.workspace_id,
            "description": row.description or "",
            "isPublic": bool(row.is_public),
            "publicToken": row.public_token,
        }
    return get_dashboard_file(dashboard_id)


async def update_dashboard_meta(
    db, dashboard_id: str, updates: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    if _resolve_backend(None) == "db":
        row = await update_dashboard_meta_db(db, dashboard_id, updates)
        if not row:
            return None
        return {
            "id": row.id,
            "workspaceId": row.workspace_id,
            "description": row.description or "",
            "isPublic": bool(row.is_public),
            "publicToken": row.public_token,
        }
    return update_dashboard_meta_file(dashboard_id, updates)


async def ensure_public_token(db, dashboard_id: str) -> Optional[str]:
    if _resolve_backend(None) == "db":
        return await ensure_public_token_db(db, dashboard_id)
    return ensure_public_token_file(dashboard_id)


async def list_widgets(db, dashboard_id: str) -> List[Dict[str, Any]]:
    if _resolve_backend(None) == "db":
        rows = await list_widgets_db(db, dashboard_id)
        return [
            {
                "id": w.id,
                "dashboardId": w.dashboard_id,
                "type": w.type,
                "title": w.title or "",
                "colorScheme": w.color_scheme,
                "layout": w.layout,
                "config": w.config or {},
            }
            for w in rows
        ]
    return list_widgets_file(dashboard_id)


async def create_widget(db, dashboard_id: str, widget: Dict[str, Any]) -> Dict[str, Any]:
    if _resolve_backend(None) == "db":
        w = await create_widget_db(db, dashboard_id, widget)
        return {
            "id": w.id,
            "dashboardId": w.dashboard_id,
            "type": w.type,
            "title": w.title or "",
            "colorScheme": w.color_scheme,
            "layout": w.layout,
            "config": w.config or {},
        }
    return create_widget_file(dashboard_id, widget)


async def update_widget(db, widget_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if _resolve_backend(None) == "db":
        w = await update_widget_db(db, widget_id, updates)
        if not w:
            return None
        return {
            "id": w.id,
            "dashboardId": w.dashboard_id,
            "type": w.type,
            "title": w.title or "",
            "colorScheme": w.color_scheme,
            "layout": w.layout,
            "config": w.config or {},
        }
    return update_widget_file(widget_id, updates)


async def delete_widget(db, widget_id: str) -> bool:
    if _resolve_backend(None) == "db":
        return await delete_widget_db(db, widget_id)
    return delete_widget_file(widget_id)


async def delete_dashboard(db, dashboard_id: str) -> None:
    if _resolve_backend(None) == "db":
        await delete_dashboard_db(db, dashboard_id)
        return
    delete_dashboard_file(dashboard_id)


async def copy_dashboard(
    db,
    source_dashboard_id: str,
    target_dashboard_id: str,
    workspace_id: str,
) -> None:
    if _resolve_backend(None) == "db":
        await copy_dashboard_db(db, source_dashboard_id, target_dashboard_id, workspace_id)
        return
    copy_dashboard_file(source_dashboard_id, target_dashboard_id)
