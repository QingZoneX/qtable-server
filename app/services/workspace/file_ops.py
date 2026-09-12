"""
Workspace file backend operations
"""
from typing import Any, Dict, List, Optional

from .helpers import (
    _ensure_workspace,
    _save_workspace,
    _normalize_workspace_id,
    _gen_id,
    _find_node,
    _find_parent,
)


def list_workspace(workspace_id: Optional[str] = None) -> Dict[str, Any]:
    """Get workspace structure for file backend"""
    if workspace_id and workspace_id != "wkbDefault":
        return _ensure_workspace()
    return _ensure_workspace()


def list_workspaces() -> List[Dict[str, Any]]:
    """List all workspaces for file backend"""
    ws = _ensure_workspace()
    return [{"id": ws.get("workspaceId", "wkbDefault"), "name": "默认空间", "rootId": ws.get("root", {}).get("id")}]


def create_folder(name: str, parent_id: str, workspace_id: Optional[str] = None) -> Dict[str, Any]:
    """Create folder in file backend"""
    if workspace_id and workspace_id != "wkbDefault":
        raise ValueError("Invalid workspace")
    
    ws = _ensure_workspace()
    root = ws["root"]
    parent = root if parent_id == root["id"] else _find_node(root.get("children", []), parent_id)
    
    if not parent or parent.get("type") in {"table", "dashboard"}:
        raise ValueError("Invalid parent for folder")
    
    folder = {"type": "folder", "id": _gen_id("fld"), "name": name, "children": []}
    parent.setdefault("children", []).append(folder)
    _save_workspace(ws)
    return folder


def create_table(
    name: str,
    parent_id: str,
    default_view_id: str = "v1",
    workspace_id: Optional[str] = None,
    template_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create table in file backend"""
    if workspace_id and workspace_id != "wkbDefault":
        raise ValueError("Invalid workspace")
    
    ws = _ensure_workspace()
    root = ws["root"]
    parent = root if parent_id == root["id"] else _find_node(root.get("children", []), parent_id)
    
    if not parent or parent.get("type") in {"table", "dashboard"}:
        raise ValueError("Invalid parent for table")
    
    table_id = _gen_id("dst")

    # Build the table data file before publishing workspace metadata. Do not
    # swallow template/init failures: a visible table must always have a core
    # schema.
    from ..smart_table_store.helpers import init_table_file

    init_table_file(table_id, template_id)

    table = {
        "type": "table",
        "id": table_id,
        "name": name,
        "defaultViewId": default_view_id,
    }
    parent.setdefault("children", []).append(table)
    _save_workspace(ws)
    return table


def create_dashboard(
    name: str,
    parent_id: str,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create dashboard in file backend"""
    if workspace_id and workspace_id != "wkbDefault":
        raise ValueError("Invalid workspace")

    ws = _ensure_workspace()
    root = ws["root"]
    parent = root if parent_id == root["id"] else _find_node(root.get("children", []), parent_id)

    if not parent or parent.get("type") in {"table", "dashboard"}:
        raise ValueError("Invalid parent for dashboard")

    dashboard_id = _gen_id("dsb")
    dashboard = {"type": "dashboard", "id": dashboard_id, "name": name}
    parent.setdefault("children", []).append(dashboard)
    _save_workspace(ws)

    try:
        from ..dashboard_store.helpers import init_dashboard_file

        init_dashboard_file(dashboard_id)
    except Exception:
        pass

    return dashboard


def rename_item(item_id: str, name: str, workspace_id: Optional[str] = None) -> bool:
    """Rename item in file backend"""
    if workspace_id and workspace_id != "wkbDefault":
        return False
    
    ws = _ensure_workspace()
    root = ws["root"]
    node = root if root["id"] == item_id else _find_node(root.get("children", []), item_id)
    
    if not node:
        return False
    
    node["name"] = name
    _save_workspace(ws)
    return True


def delete_item(item_id: str, workspace_id: Optional[str] = None) -> bool:
    """Delete item in file backend"""
    if workspace_id and workspace_id != "wkbDefault":
        return False
    
    ws = _ensure_workspace()
    root = ws["root"]
    node = root if root["id"] == item_id else _find_node(root.get("children", []), item_id)
    parent_children = _find_parent(root.get("children", []), item_id)
    
    if not parent_children:
        return False
    
    before = len(parent_children)
    parent_children[:] = [c for c in parent_children if c.get("id") != item_id]
    _save_workspace(ws)
    if node and node.get("type") == "dashboard":
        try:
            from ..dashboard_store.file_backend import delete_dashboard_file

            delete_dashboard_file(item_id)
        except Exception:
            pass
    return len(parent_children) < before


def move_item(item_id: str, new_parent_id: str, workspace_id: Optional[str] = None) -> bool:
    """Move item in file backend"""
    if workspace_id and workspace_id != "wkbDefault":
        return False
    
    ws = _ensure_workspace()
    root = ws["root"]
    node_parent = _find_parent(root.get("children", []), item_id)
    node = _find_node(root.get("children", []), item_id)
    new_parent = root if root["id"] == new_parent_id else _find_node(root.get("children", []), new_parent_id)
    
    if not node_parent or not node or not new_parent or new_parent.get("type") in {"table", "dashboard"}:
        return False
    
    node_parent.remove(node)
    new_parent.setdefault("children", []).append(node)
    _save_workspace(ws)
    return True


def copy_table(
    table_id: str,
    new_parent_id: str,
    new_name: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Copy table in file backend"""
    if workspace_id and workspace_id != "wkbDefault":
        return None
    
    ws = _ensure_workspace()
    root = ws["root"]
    node = _find_node(root.get("children", []), table_id)
    new_parent = root if root["id"] == new_parent_id else _find_node(root.get("children", []), new_parent_id)
    
    if not node or node.get("type") != "table" or not new_parent or new_parent.get("type") == "table":
        return None
    
    new_table_id = _gen_id("dst")
    table_meta = {
        "type": "table",
        "id": new_table_id,
        "name": new_name or f'{node.get("name", "Table")} Copy',
        "defaultViewId": node.get("defaultViewId", "v1")
    }
    new_parent.setdefault("children", []).append(table_meta)
    _save_workspace(ws)
    
    # duplicate table data file
    try:
        from ..smart_table_store.helpers import _table_file_path, _load_table_json, _save_table_json
        src_store = _load_table_json(table_id)
        _save_table_json(new_table_id, src_store)
    except Exception:
        pass
    
    return table_meta


def copy_dashboard(
    dashboard_id: str,
    new_parent_id: str,
    new_name: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Copy dashboard in file backend"""
    if workspace_id and workspace_id != "wkbDefault":
        return None

    ws = _ensure_workspace()
    root = ws["root"]
    node = _find_node(root.get("children", []), dashboard_id)
    new_parent = root if root["id"] == new_parent_id else _find_node(root.get("children", []), new_parent_id)

    if not node or node.get("type") != "dashboard" or not new_parent or new_parent.get("type") in {"table", "dashboard"}:
        return None

    new_dashboard_id = _gen_id("dsb")
    dashboard_meta = {
        "type": "dashboard",
        "id": new_dashboard_id,
        "name": new_name or f'{node.get("name", "Dashboard")} Copy',
    }
    new_parent.setdefault("children", []).append(dashboard_meta)
    _save_workspace(ws)

    try:
        from ..dashboard_store.file_backend import copy_dashboard_file

        copy_dashboard_file(dashboard_id, new_dashboard_id)
    except Exception:
        pass

    return dashboard_meta
