"""
Workspace helpers module
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
WORKSPACE_PATH = os.path.join(DATA_DIR, "workspace.json")


def _ensure_workspace() -> Dict[str, Any]:
    """Ensure workspace file exists"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(WORKSPACE_PATH):
        payload = {
            "workspaceId": "wkbDefault",
            "root": {
                "id": "fldRoot",
                "name": "Root",
                "children": [
                    {"type": "table", "id": "dstDefault", "name": "Projects", "defaultViewId": "v1"}
                ],
            },
        }
        with open(WORKSPACE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return payload
    with open(WORKSPACE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_workspace(ws: Dict[str, Any]) -> None:
    """Save workspace to file"""
    with open(WORKSPACE_PATH, "w", encoding="utf-8") as f:
        json.dump(ws, f, ensure_ascii=False, indent=2)


def _normalize_workspace_id(workspace_id: Optional[str]) -> str:
    """Normalize workspace ID"""
    return workspace_id or "wkbDefault"


def get_default_workspace_id(user_id: int) -> str:
    """Get default workspace ID for user"""
    return f"wkbUser{user_id}"


def _gen_id(prefix: str) -> str:
    """Generate ID with prefix"""
    return f"{prefix}{int(time.time() * 1000)}"


def _find_node(children: List[Dict[str, Any]], node_id: str) -> Optional[Dict[str, Any]]:
    """Find node in workspace tree"""
    for c in children:
        if c.get("id") == node_id:
            return c
        if c.get("type") == "folder":
            found = _find_node(c.get("children", []), node_id)
            if found:
                return found
    return None


def _find_parent(children: List[Dict[str, Any]], node_id: str) -> Optional[List[Dict[str, Any]]]:
    """Find parent of node in workspace tree"""
    for c in children:
        if c.get("id") == node_id:
            return children
        if c.get("type") == "folder":
            parent = _find_parent(c.get("children", []), node_id)
            if parent:
                return parent
    return None
