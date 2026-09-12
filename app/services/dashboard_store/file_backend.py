from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .helpers import (
    _dashboard_file_path,
    _generate_id,
    _load_dashboard_json,
    _save_dashboard_json,
    generate_public_token,
    init_dashboard_file,
)


def get_dashboard_file(dashboard_id: str) -> Dict[str, Any]:
    return _load_dashboard_json(dashboard_id)


def update_dashboard_meta_file(
    dashboard_id: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    payload = _load_dashboard_json(dashboard_id)
    for key in ["description", "isPublic", "publicToken"]:
        if key in updates:
            payload[key] = updates[key]
    _save_dashboard_json(dashboard_id, payload)
    return payload


def ensure_public_token_file(dashboard_id: str) -> str:
    payload = _load_dashboard_json(dashboard_id)
    token = payload.get("publicToken")
    if isinstance(token, str) and token:
        return token
    token = generate_public_token()
    payload["publicToken"] = token
    _save_dashboard_json(dashboard_id, payload)
    return token


def list_widgets_file(dashboard_id: str) -> List[Dict[str, Any]]:
    payload = _load_dashboard_json(dashboard_id)
    widgets = payload.get("widgets", [])
    return widgets if isinstance(widgets, list) else []


def create_widget_file(dashboard_id: str, widget: Dict[str, Any]) -> Dict[str, Any]:
    payload = _load_dashboard_json(dashboard_id)
    widgets: List[Dict[str, Any]] = payload.get("widgets", [])
    widget_id = _generate_id("wdg")
    next_widget = {
        "id": widget_id,
        "type": widget.get("type"),
        "title": widget.get("title") or "",
        "colorScheme": widget.get("colorScheme"),
        "layout": widget.get("layout") or {"x": 0, "y": 0, "w": 6, "h": 8},
        "config": widget.get("config") or {},
    }
    widgets.append(next_widget)
    payload["widgets"] = widgets
    _save_dashboard_json(dashboard_id, payload)
    return next_widget


def update_widget_file(widget_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for file_name in os.listdir(os.path.dirname(_dashboard_file_path("x"))):
        if not file_name.endswith(".json"):
            continue
        dashboard_id = file_name[:-5]
        payload = _load_dashboard_json(dashboard_id)
        widgets: List[Dict[str, Any]] = payload.get("widgets", [])
        for idx, w in enumerate(widgets):
            if w.get("id") != widget_id:
                continue
            next_widget = dict(w)
            for key, out_key in [
                ("type", "type"),
                ("title", "title"),
                ("colorScheme", "colorScheme"),
                ("layout", "layout"),
                ("config", "config"),
            ]:
                if key in updates:
                    next_widget[out_key] = updates[key]
            widgets[idx] = next_widget
            payload["widgets"] = widgets
            _save_dashboard_json(dashboard_id, payload)
            return next_widget
    return None


def delete_widget_file(widget_id: str) -> bool:
    for file_name in os.listdir(os.path.dirname(_dashboard_file_path("x"))):
        if not file_name.endswith(".json"):
            continue
        dashboard_id = file_name[:-5]
        payload = _load_dashboard_json(dashboard_id)
        widgets: List[Dict[str, Any]] = payload.get("widgets", [])
        before = len(widgets)
        widgets = [w for w in widgets if w.get("id") != widget_id]
        if len(widgets) == before:
            continue
        payload["widgets"] = widgets
        _save_dashboard_json(dashboard_id, payload)
        return True
    return False


def delete_dashboard_file(dashboard_id: str) -> None:
    path = _dashboard_file_path(dashboard_id)
    if os.path.exists(path):
        os.remove(path)


def copy_dashboard_file(source_dashboard_id: str, target_dashboard_id: str) -> None:
    init_dashboard_file(target_dashboard_id)
    payload = _load_dashboard_json(source_dashboard_id)
    payload["id"] = target_dashboard_id
    widgets = payload.get("widgets", [])
    if isinstance(widgets, list):
        copied: List[Dict[str, Any]] = []
        for w in widgets:
            next_w = dict(w)
            next_w["id"] = _generate_id("wdg")
            copied.append(next_w)
        payload["widgets"] = copied
    payload["publicToken"] = None
    payload["isPublic"] = False
    _save_dashboard_json(target_dashboard_id, payload)
