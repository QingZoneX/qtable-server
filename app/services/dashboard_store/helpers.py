from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DASHBOARDS_DIR = os.path.join(DATA_DIR, "dashboards")


def _ensure_dashboards_dir() -> None:
    os.makedirs(DASHBOARDS_DIR, exist_ok=True)


def _dashboard_file_path(dashboard_id: str) -> str:
    _ensure_dashboards_dir()
    return os.path.join(DASHBOARDS_DIR, f"{dashboard_id}.json")


def dashboard_file_exists(dashboard_id: str) -> bool:
    return os.path.exists(_dashboard_file_path(dashboard_id))


def init_dashboard_file(dashboard_id: str) -> None:
    path = _dashboard_file_path(dashboard_id)
    if os.path.exists(path):
        return
    payload = {
        "id": dashboard_id,
        "description": "",
        "isPublic": False,
        "publicToken": None,
        "widgets": [],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_dashboard_json(dashboard_id: str) -> Dict[str, Any]:
    path = _dashboard_file_path(dashboard_id)
    if not os.path.exists(path):
        init_dashboard_file(dashboard_id)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_dashboard_json(dashboard_id: str, payload: Dict[str, Any]) -> None:
    path = _dashboard_file_path(dashboard_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _generate_id(prefix: str) -> str:
    return f"{prefix}{int(time.time() * 1000)}"


def generate_public_token() -> str:
    return secrets.token_urlsafe(24)


def normalize_dashboard_id(dashboard_id: Optional[str]) -> Optional[str]:
    if not dashboard_id:
        return None
    if dashboard_id.startswith("dsb"):
        return dashboard_id
    return None
