"""
File backend operations for smart table store
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

from .helpers import (
    _load_table_json,
    _save_table_json,
    _generate_id,
    _deep_clone,
)

from app.services.auto_number_engine import (
    allocate_auto_number_values,
    is_auto_number_field,
    normalize_auto_number_property,
)


async def get_full_store_for_table(table_id: str) -> Dict[str, Any]:
    """Get full table store for file backend"""
    store = _load_table_json(table_id)
    fields = store.get("fields", [])
    hidden_field_ids = [
        f["id"]
        for f in fields
        if isinstance(f.get("property"), dict) and f["property"].get("hidden", False)
    ]
    return {
        "fields": fields,
        "records": store.get("records", []),
        "views": store.get("views", []),
        "hiddenFieldIds": hidden_field_ids,
        "filters": store.get("filters", []),
        "sorts": store.get("sorts", []),
        "groupConfig": store.get("groupConfig", {"fieldId": None, "order": "asc"}),
    }


async def create_record_file(table_id: str) -> Dict[str, Any]:
    """Create a new record and allocate auto numbers."""
    store = _load_table_json(table_id)
    fields: List[Dict[str, Any]] = store.get("fields", [])
    record_id = _generate_id("r")
    data: Dict[str, Any] = {}
    for field in fields:
        fid = field.get("id")
        if fid is None:
            continue
        if is_auto_number_field(field):
            values, next_property = allocate_auto_number_values(
                field.get("property"), 1
            )
            data[fid] = values[0]
            field["property"] = next_property
        else:
            data[fid] = None
    store.setdefault("records", [])
    store["records"].append({"id": record_id, **data})
    _save_table_json(table_id, store)
    return {"id": record_id, **data}


async def create_records_with_data_file(
    table_id: str, records_data: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Batch create rows; client-supplied auto-number values are ignored."""
    store = _load_table_json(table_id)
    fields: List[Dict[str, Any]] = store.get("fields", [])
    amount = len(records_data)
    auto_values: Dict[str, List[int]] = {}
    for field in fields:
        fid = field.get("id")
        if fid and is_auto_number_field(field):
            values, next_property = allocate_auto_number_values(
                field.get("property"), amount
            )
            auto_values[fid] = values
            field["property"] = next_property

    created: List[Dict[str, Any]] = []
    for index, row_data in enumerate(records_data):
        record_id = _generate_id("r")
        data: Dict[str, Any] = {}
        for field in fields:
            fid = field.get("id")
            if fid is None:
                continue
            if fid in auto_values:
                data[fid] = auto_values[fid][index]
            else:
                data[fid] = row_data.get(fid) if fid in row_data else None
        created.append({"id": record_id, **data})

    store.setdefault("records", []).extend(created)
    _save_table_json(table_id, store)
    return created


async def create_records_file(table_id: str, count: int) -> List[Dict[str, Any]]:
    """Create multiple records and allocate stable auto numbers."""
    store = _load_table_json(table_id)
    fields: List[Dict[str, Any]] = store.get("fields", [])
    amount = max(1, min(999, int(count)))
    auto_values: Dict[str, List[int]] = {}
    for field in fields:
        fid = field.get("id")
        if fid and is_auto_number_field(field):
            values, next_property = allocate_auto_number_values(
                field.get("property"), amount
            )
            auto_values[fid] = values
            field["property"] = next_property

    base_id = int(time.time() * 1000)
    created: List[Dict[str, Any]] = []
    for i in range(amount):
        record_id = f"r{base_id + i}"
        data: Dict[str, Any] = {}
        for field in fields:
            fid = field.get("id")
            if fid is not None:
                data[fid] = auto_values[fid][i] if fid in auto_values else None
        created.append({"id": record_id, **data})
    store.setdefault("records", []).extend(created)
    _save_table_json(table_id, store)
    return created


async def update_record_file(
    table_id: str, record_id: str, field_id: str, value: Any
) -> Optional[Dict[str, Any]]:
    """Update a record in file backend"""
    store = _load_table_json(table_id)
    records: List[Dict[str, Any]] = store.get("records", [])
    for r in records:
        if r.get("id") == record_id:
            r[field_id] = value
            _save_table_json(table_id, store)
            return r
    return None


async def apply_record_patches_file(table_id: str, patches: List[Dict[str, Any]]) -> int:
    """Apply record patches in file backend"""
    store = _load_table_json(table_id)
    records: List[Dict[str, Any]] = store.get("records", [])
    count = 0
    idmap = {r.get("id"): r for r in records}
    for p in patches:
        rec = idmap.get(p.get("recordId"))
        if rec is None:
            continue
        rec[p.get("fieldId")] = p.get("value")
        count += 1
    _save_table_json(table_id, store)
    return count


async def add_field_file(
    table_id: str, field_data: Dict[str, Any], index: Optional[int] = None
) -> Dict[str, Any]:
    """Add field and backfill existing rows for auto-number fields."""
    store = _load_table_json(table_id)
    fields: List[Dict[str, Any]] = store.setdefault("fields", [])
    next_field = dict(field_data)
    if is_auto_number_field(next_field):
        next_field["property"] = normalize_auto_number_property(
            next_field.get("property")
        )
        # nextNumber is internal server state, never a client seed.
        next_field["property"]["nextNumber"] = next_field["property"]["start"]
        records: List[Dict[str, Any]] = store.get("records", [])
        values, next_property = allocate_auto_number_values(
            next_field["property"], len(records)
        )
        for record, number in zip(records, values):
            record[next_field["id"]] = number
        next_field["property"] = next_property

    if index is not None and 0 <= index <= len(fields):
        fields.insert(index, next_field)
    else:
        fields.append(next_field)
    _save_table_json(table_id, store)
    return next_field


async def update_field_file(
    table_id: str, field_id: str, updates: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Update field metadata while preserving auto-number sequence state."""
    store = _load_table_json(table_id)
    fields: List[Dict[str, Any]] = store.get("fields", [])
    for field in fields:
        if field.get("id") != field_id:
            continue

        old_type = field.get("type")
        next_type = updates.get("type", old_type)
        next_property = updates.get("property", field.get("property"))
        if next_type == "autoNumber":
            if old_type == "autoNumber":
                current_property = normalize_auto_number_property(
                    field.get("property")
                )
                requested_property = dict(next_property or {})
                requested_property["start"] = current_property["start"]
                next_property = normalize_auto_number_property(
                    requested_property,
                    preserve_next=int(current_property["nextNumber"]),
                )
            else:
                next_property = normalize_auto_number_property(next_property)
                next_property["nextNumber"] = next_property["start"]
                records: List[Dict[str, Any]] = store.get("records", [])
                values, next_property = allocate_auto_number_values(
                    next_property, len(records)
                )
                for record, number in zip(records, values):
                    record[field_id] = number

        field.update({k: v for k, v in updates.items() if v is not None})
        if next_type == "autoNumber":
            field["property"] = next_property
        _save_table_json(table_id, store)
        return field
    return None


async def delete_field_file(table_id: str, field_id: str) -> bool:
    """Delete a field in file backend"""
    store = _load_table_json(table_id)
    fields: List[Dict[str, Any]] = store.get("fields", [])
    before = len(fields)
    fields[:] = [f for f in fields if f.get("id") != field_id]
    if len(fields) == before:
        return False
    # remove from records
    for r in store.get("records", []):
        if field_id in r:
            r.pop(field_id, None)
    _save_table_json(table_id, store)
    return True


async def delete_record_file(table_id: str, record_id: str) -> bool:
    """Delete a record in file backend"""
    store = _load_table_json(table_id)
    records: List[Dict[str, Any]] = store.get("records", [])
    before = len(records)
    records[:] = [r for r in records if r.get("id") != record_id]
    _save_table_json(table_id, store)
    return len(records) < before


async def reorder_fields_file(table_id: str, field_order: List[str]) -> bool:
    """Reorder fields in file backend"""
    store = _load_table_json(table_id)
    fields: List[Dict[str, Any]] = store.get("fields", [])
    id_to_field = {f["id"]: f for f in fields}
    new_fields = [id_to_field[fid] for fid in field_order if fid in id_to_field]
    # append any missing at end
    for f in fields:
        if f["id"] not in field_order:
            new_fields.append(f)
    store["fields"] = new_fields
    _save_table_json(table_id, store)
    return True


async def update_field_visibility_file(table_id: str, hidden_field_ids: List[str]) -> bool:
    """Update field visibility in file backend"""
    store = _load_table_json(table_id)
    for f in store.get("fields", []):
        prop = dict(f.get("property") or {})
        prop["hidden"] = f.get("id") in hidden_field_ids
        f["property"] = prop
    _save_table_json(table_id, store)
    return True


async def update_filters_file(table_id: str, filters: List[Dict[str, Any]]) -> bool:
    """Update filters in file backend"""
    store = _load_table_json(table_id)
    store["filters"] = filters
    _save_table_json(table_id, store)
    return True


async def clear_filters_file(table_id: str) -> bool:
    """Clear all filters in file backend"""
    store = _load_table_json(table_id)
    store["filters"] = []
    _save_table_json(table_id, store)
    return True


async def update_sorts_file(table_id: str, sorts: List[Dict[str, Any]]) -> bool:
    """Update sorts in file backend"""
    store = _load_table_json(table_id)
    store["sorts"] = sorts
    _save_table_json(table_id, store)
    return True


async def clear_sorts_file(table_id: str) -> bool:
    """Clear all sorts in file backend"""
    store = _load_table_json(table_id)
    store["sorts"] = []
    _save_table_json(table_id, store)
    return True


async def update_group_config_file(table_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Update group config in file backend"""
    store = _load_table_json(table_id)
    store["groupConfig"] = {"fieldId": config.get("fieldId"), "order": config.get("order", "asc")}
    _save_table_json(table_id, store)
    return store["groupConfig"]


async def update_view_config_file(
    table_id: str, view_id: str, config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Update view config in file backend"""
    store = _load_table_json(table_id)
    views: List[Dict[str, Any]] = store.get("views", [])
    for v in views:
        if v.get("id") == view_id:
            v["config"] = config
            _save_table_json(table_id, store)
            return v
    return None


async def create_view_file(table_id: str, view_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a view in file backend"""
    store = _load_table_json(table_id)
    views: List[Dict[str, Any]] = store.setdefault("views", [])
    views.append(view_data)
    _save_table_json(table_id, store)
    if len(views) == 1:
        _set_file_table_default_view_id(table_id, view_data["id"])
    return view_data


async def rename_view_file(table_id: str, view_id: str, name: str) -> Optional[Dict[str, Any]]:
    """Rename a view in file backend"""
    store = _load_table_json(table_id)
    views: List[Dict[str, Any]] = store.get("views", [])
    for view in views:
        if view.get("id") == view_id:
            view["name"] = name
            _save_table_json(table_id, store)
            return view
    return None


async def copy_view_file(
    table_id: str, source_view_id: str, new_view_data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Copy a view in file backend"""
    store = _load_table_json(table_id)
    views: List[Dict[str, Any]] = store.get("views", [])
    source = next((v for v in views if v.get("id") == source_view_id), None)
    if not source:
        return None
    views.append(new_view_data)
    _save_table_json(table_id, store)
    return new_view_data


async def delete_view_file(table_id: str, view_id: str) -> bool:
    """Delete a view in file backend"""
    store = _load_table_json(table_id)
    views: List[Dict[str, Any]] = store.get("views", [])
    if len(views) <= 1:
        return False
    removed = next((v for v in views if v.get("id") == view_id), None)
    if not removed:
        return False
    store["views"] = [v for v in views if v.get("id") != view_id]
    _save_table_json(table_id, store)
    if removed.get("id"):
        first_view = store["views"][0] if store["views"] else None
        if first_view and first_view.get("id"):
            _set_file_table_default_view_id(table_id, first_view["id"])
    return True


def _set_file_table_default_view_id(table_id: str, view_id: str) -> None:
    """Set default view ID for a table in file backend"""
    from .helpers import _workspace_file_path
    
    path = _workspace_file_path()
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    root = payload.get("root")
    if not isinstance(root, dict):
        return
    from .helpers import _find_workspace_table_node
    table_node = _find_workspace_table_node(root, table_id)
    if not table_node:
        return
    if table_node.get("defaultViewId") == view_id:
        return
    table_node["defaultViewId"] = view_id
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _find_workspace_table_node(node: Dict[str, Any], table_id: str) -> Optional[Dict[str, Any]]:
    """Find a table node in workspace tree"""
    if node.get("id") == table_id and node.get("type") == "table":
        return node
    for child in node.get("children", []) or []:
        found = _find_workspace_table_node(child, table_id)
        if found:
            return found
    return None
