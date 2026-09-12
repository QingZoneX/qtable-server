"""
Helpers for smart table store
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
TABLES_DIR = os.path.join(DATA_DIR, "tables")
TEMPLATE_PATH = os.path.join(DATA_DIR, "smart_table.json")
TEMPLATES_DIR = os.path.join(DATA_DIR, "templates")
TEMPLATE_INDEX_PATH = os.path.join(TEMPLATES_DIR, "index.json")


def _ensure_tables_dir() -> None:
    os.makedirs(TABLES_DIR, exist_ok=True)


def _table_file_path(table_id: str) -> str:
    _ensure_tables_dir()
    return os.path.join(TABLES_DIR, f"{table_id}.json")


def table_file_exists(table_id: str) -> bool:
    path = _table_file_path(table_id)
    return os.path.exists(path)


def resolve_table_id(doc_id: Optional[str]) -> Optional[str]:
    if not doc_id:
        return None
    if doc_id == "default":
        return "dstDefault"
    if doc_id.startswith("dst"):
        return doc_id
    if table_file_exists(doc_id):
        return doc_id
    return None


def normalize_table_id(table_id: Optional[str]) -> str:
    return table_id or "dstDefault"


def _load_table_json(table_id: str) -> Dict[str, Any]:
    path = _table_file_path(table_id)
    if not os.path.exists(path):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            template = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
        return template
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_table_json(table_id: str, payload: Dict[str, Any]) -> None:
    path = _table_file_path(table_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def init_table_file(table_id: str, template_id: Optional[str] = None) -> None:
    path = _table_file_path(table_id)
    if os.path.exists(path):
        return
    template = generate_table_from_template(template_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)


def _generate_id(prefix: str) -> str:
    return f"{prefix}{int(time.time() * 1000)}"


def _deep_clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


def get_template_index() -> List[Dict[str, Any]]:
    """Get the template index with metadata for all available templates"""
    if not os.path.exists(TEMPLATE_INDEX_PATH):
        return []
    with open(TEMPLATE_INDEX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_template(template_id: str) -> Optional[Dict[str, Any]]:
    """Load a specific template by its ID"""
    template_index = get_template_index()
    for entry in template_index:
        if entry.get("id") == template_id:
            filename = entry.get("filename")
            if not filename:
                continue
            filepath = os.path.join(TEMPLATES_DIR, filename)
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
    return None


def generate_table_from_template(template_id: Optional[str]) -> Dict[str, Any]:
    """
    Generate a table JSON payload from a template.
    If template_id is None or not found, falls back to the default template (smart_table.json).
    The resulting table has empty records, filters, and sorts.
    """
    template = None
    if template_id:
        template = load_template(template_id)

    if template is None:
        # Fallback to default template
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            template = json.load(f)

    template["records"] = []
    template["filters"] = []
    template["sorts"] = []
    template["groupConfig"] = template.get("groupConfig") or {
        "fieldId": None,
        "order": "asc",
    }
    return template
