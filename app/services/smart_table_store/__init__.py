"""
Smart Table Store - Modular interface.

Derived/formula and relation fields are integrated here, above both persistence
backends, so DB and JSON-file tables share the same validation and write rules.
"""
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField
from app.services.formula_engine import (
    FormulaValidationError,
    formula_dependents,
    is_formula_field,
    materialize_records,
    materialize_store,
    validate_formula_schema,
)
from app.services.auto_number_engine import (
    AutoNumberValidationError,
    is_auto_number_field,
    validate_auto_number_field,
)
from app.services.relation_engine import (
    RelationValidationError,
    is_relation_field,
    validate_relation_schema,
)
from app.services.relation_store import (
    normalize_relation_payload_db,
    normalize_relation_payload_file,
    normalize_relation_rows_db,
    normalize_relation_rows_file,
    validate_relation_field_db,
    validate_relation_field_file,
)
from app.services.row_permissions import ensure_permission_field_change_safe

# Import helper functions
from .helpers import (
    resolve_table_id,
    normalize_table_id,
    table_file_exists,
    init_table_file,
)

# Import file backend functions. Formula/relation-sensitive operations are
# imported under private aliases and wrapped below.
from .file_backend import (
    get_full_store_for_table as _get_full_store_for_table,
    create_record_file as _create_record_file,
    create_records_file as _create_records_file,
    create_records_with_data_file as _create_records_with_data_file,
    update_record_file as _update_record_file,
    delete_record_file,
    apply_record_patches_file as _apply_record_patches_file,
    add_field_file as _add_field_file,
    update_field_file as _update_field_file,
    delete_field_file as _delete_field_file,
    reorder_fields_file,
    update_field_visibility_file,
    update_filters_file,
    clear_filters_file,
    update_sorts_file,
    clear_sorts_file,
    update_group_config_file,
    update_view_config_file,
    create_view_file,
    rename_view_file,
    copy_view_file,
    delete_view_file,
)

# Import database backend functions. Formula/relation-sensitive operations are
# imported under private aliases and wrapped below.
from .db_backend import (
    table_has_data,
    init_table_db,
    delete_table_data_db,
    copy_table_db,
    get_full_store as _get_full_store,
    create_record as _create_record,
    create_records as _create_records,
    create_records_with_data as _create_records_with_data,
    update_record as _update_record,
    update_record_fields as _update_record_fields,
    delete_record as _delete_record,
    apply_record_patches as _apply_record_patches,
    add_field as _add_field,
    update_field as _update_field,
    delete_field as _delete_field,
    reorder_fields,
    update_field_visibility,
    get_filters,
    add_filter,
    update_filter,
    delete_filter,
    update_filters,
    clear_filters,
    get_sorts,
    add_sort,
    delete_sort,
    update_sorts,
    clear_sorts,
    update_group_config,
    update_view_config,
    create_view,
    rename_view,
    copy_view,
    delete_view,
    _set_db_table_default_view_id,
)

_MISSING = object()


def _field_to_dict(field: TableField) -> Dict[str, Any]:
    return {
        "id": field.id,
        "name": field.name,
        "type": field.type,
        "options": field.options,
        "property": field.property,
    }


async def _db_fields(db: AsyncSession, table_id: Optional[str]) -> List[Dict[str, Any]]:
    resolved_table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableField)
        .where(TableField.table_id == resolved_table_id)
        .order_by(TableField.order_index)
    )
    return [_field_to_dict(field) for field in result.scalars().all()]


async def _file_fields(table_id: str) -> List[Dict[str, Any]]:
    store = await _get_full_store_for_table(table_id)
    return [dict(field) for field in store.get("fields", [])]


def _formula_field_ids(fields: Sequence[Dict[str, Any]]) -> set[str]:
    return {
        str(field.get("id"))
        for field in fields
        if field.get("id") and is_formula_field(field)
    }


def _ensure_field_is_writable(fields: Sequence[Dict[str, Any]], field_id: str) -> None:
    field = next((item for item in fields if item.get("id") == field_id), None)
    if field and is_formula_field(field):
        raise FormulaValidationError(
            f"Formula field '{field.get('name') or field_id}' is read-only"
        )
    if field and is_auto_number_field(field):
        raise AutoNumberValidationError(
            f"Auto-number field '{field.get('name') or field_id}' is read-only"
        )


def _strip_formula_values(
    values: Dict[str, Any], fields: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    readonly_ids = _formula_field_ids(fields) | {
        str(field.get("id"))
        for field in fields
        if field.get("id") and is_auto_number_field(field)
    }
    return {key: value for key, value in values.items() if key not in readonly_ids}


def _normalize_text_value(value: Any) -> Any:
    """Trim leading/trailing whitespace for text fields only.

    str.strip() removes spaces, tabs and leading/trailing blank lines while
    preserving intentional whitespace/newlines inside the value.
    """
    return value.strip() if isinstance(value, str) else value


def _normalize_text_payload(
    values: Dict[str, Any], fields: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    field_types = {
        str(field.get("id")): str(field.get("type") or "")
        for field in fields
        if field.get("id") is not None
    }
    return {
        key: _normalize_text_value(value)
        if field_types.get(str(key)) == "text"
        else value
        for key, value in values.items()
    }


def _validate_field_add(fields: Sequence[Dict[str, Any]], field_data: Dict[str, Any]) -> None:
    candidates = [*fields, dict(field_data)]
    validate_formula_schema(candidates)
    validate_relation_schema(candidates)
    validate_auto_number_field(field_data)


def _field_update_candidates(
    fields: Sequence[Dict[str, Any]],
    field_id: str,
    updates: Dict[str, Any],
    *,
    ignore_none: bool,
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    candidates: List[Dict[str, Any]] = []
    updated_field: Optional[Dict[str, Any]] = None
    for field in fields:
        candidate = dict(field)
        if field.get("id") == field_id:
            for key, value in updates.items():
                if ignore_none and value is None:
                    continue
                candidate[key] = value
            updated_field = candidate
        candidates.append(candidate)
    return candidates, updated_field


def _validate_field_update(
    fields: Sequence[Dict[str, Any]],
    field_id: str,
    updates: Dict[str, Any],
    *,
    ignore_none: bool,
) -> Optional[Dict[str, Any]]:
    candidates, updated_field = _field_update_candidates(
        fields, field_id, updates, ignore_none=ignore_none
    )
    if updated_field is not None:
        validate_formula_schema(candidates)
        validate_relation_schema(candidates)
        validate_auto_number_field(updated_field)
    return updated_field


def _ensure_field_can_be_deleted(fields: Sequence[Dict[str, Any]], field_id: str) -> None:
    dependents = formula_dependents(fields, field_id)
    if dependents:
        preview = ", ".join(dependents[:3])
        if len(dependents) > 3:
            preview += f" and {len(dependents) - 3} more"
        raise FormulaValidationError(
            f"Field '{field_id}' is referenced by formula field(s): {preview}"
        )


def _materialize_one(
    fields: Sequence[Dict[str, Any]], record: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if record is None:
        return None
    rows = materialize_records(fields, [record])
    return rows[0] if rows else record


# ---------------------------------------------------------------------------
# Read wrappers: formulas are derived from current source values. Relation
# values intentionally remain record IDs; labels are resolved by relationOptions.
# ---------------------------------------------------------------------------

async def get_full_store(db: AsyncSession, table_id: Optional[str] = None) -> Dict[str, Any]:
    return materialize_store(await _get_full_store(db, table_id))


async def get_full_store_for_table(table_id: str) -> Dict[str, Any]:
    return materialize_store(await _get_full_store_for_table(table_id))


# ---------------------------------------------------------------------------
# Database backend write wrappers.
# ---------------------------------------------------------------------------

async def create_record(
    db: AsyncSession,
    table_id: Optional[str] = None,
    created_by_user_id: Optional[int] = None,
    *,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    created = await _create_record(
        db,
        table_id,
        created_by_user_id,
        actor_type=actor_type,
        source=source,
        trace_id=trace_id,
    )
    return _materialize_one(await _db_fields(db, table_id), created) or created

async def create_records(
    db: AsyncSession,
    table_id: Optional[str],
    count: int,
    created_by_user_id: Optional[int] = None,
    *,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    created = await _create_records(
        db,
        table_id,
        count,
        created_by_user_id,
        actor_type=actor_type,
        source=source,
        trace_id=trace_id,
    )
    return materialize_records(await _db_fields(db, table_id), created)

async def create_records_with_data(
    db: AsyncSession,
    table_id: Optional[str],
    records_data: List[Dict[str, Any]],
    created_by_user_id: Optional[int] = None,
    *,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    fields = await _db_fields(db, table_id)
    sanitized = [
        _normalize_text_payload(_strip_formula_values(dict(row), fields), fields)
        for row in records_data
    ]
    normalized = await normalize_relation_rows_db(
        db,
        fields,
        sanitized,
        user_id=created_by_user_id,
    )
    created = await _create_records_with_data(
        db,
        table_id,
        normalized,
        created_by_user_id,
        actor_type=actor_type,
        source=source,
        trace_id=trace_id,
    )
    return materialize_records(fields, created)

async def update_record(
    db: AsyncSession,
    table_id: Optional[str],
    record_id: str,
    field_id: str,
    value: Any,
    user_id: Optional[int] = None,
    *,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    fields = await _db_fields(db, table_id)
    _ensure_field_is_writable(fields, field_id)
    text_normalized = _normalize_text_payload({field_id: value}, fields)
    normalized = await normalize_relation_payload_db(
        db,
        fields,
        text_normalized,
        user_id=user_id,
    )
    updated = await _update_record(
        db,
        table_id,
        record_id,
        field_id,
        normalized.get(field_id, text_normalized.get(field_id, value)),
        user_id=user_id,
        actor_type=actor_type,
        source=source,
        trace_id=trace_id,
    )
    return _materialize_one(fields, updated)

async def update_record_fields(
    db: AsyncSession,
    table_id: Optional[str],
    record_id: str,
    fields_data: Dict[str, Any],
    user_id: Optional[int] = None,
    *,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    fields = await _db_fields(db, table_id)
    sanitized = _normalize_text_payload(
        _strip_formula_values(dict(fields_data), fields), fields
    )
    normalized = await normalize_relation_payload_db(
        db,
        fields,
        sanitized,
        user_id=user_id,
    )
    updated = await _update_record_fields(
        db,
        table_id,
        record_id,
        normalized,
        user_id=user_id,
        actor_type=actor_type,
        source=source,
        trace_id=trace_id,
    )
    return _materialize_one(fields, updated)

async def apply_record_patches(
    db: AsyncSession,
    table_id: Optional[str],
    patches: List[Dict[str, Any]],
    user_id: Optional[int] = None,
    *,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> int:
    fields = await _db_fields(db, table_id)
    normalized_patches: List[Dict[str, Any]] = []
    for patch in patches:
        next_patch = dict(patch)
        field_id = patch.get("fieldId")
        if isinstance(field_id, str):
            _ensure_field_is_writable(fields, field_id)
            text_normalized = _normalize_text_payload(
                {field_id: patch.get("value")}, fields
            )
            normalized = await normalize_relation_payload_db(
                db,
                fields,
                text_normalized,
                user_id=user_id,
            )
            next_patch["value"] = normalized.get(
                field_id, text_normalized.get(field_id, patch.get("value"))
            )
        normalized_patches.append(next_patch)
    return await _apply_record_patches(
        db,
        table_id,
        normalized_patches,
        user_id=user_id,
        actor_type=actor_type,
        source=source,
        trace_id=trace_id,
    )

async def add_field(
    db: AsyncSession,
    table_id: Optional[str],
    field_data: Dict[str, Any],
    index: Optional[int] = None,
) -> Dict[str, Any]:
    fields = await _db_fields(db, table_id)
    _validate_field_add(fields, field_data)
    await validate_relation_field_db(db, table_id, field_data)
    return await _add_field(db, table_id, field_data, index)


async def update_field(
    db: AsyncSession,
    table_id: Optional[str],
    field_id: str,
    updates: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    fields = await _db_fields(db, table_id)
    next_type = (
        str(updates.get("type"))
        if isinstance(updates, dict) and updates.get("type") is not None
        else None
    )
    await ensure_permission_field_change_safe(
        db,
        normalize_table_id(table_id),
        field_id,
        next_type=next_type,
    )
    updated_field = _validate_field_update(fields, field_id, updates, ignore_none=False)
    if updated_field is not None:
        await validate_relation_field_db(db, table_id, updated_field)
    return await _update_field(db, table_id, field_id, updates)


async def delete_field(
    db: AsyncSession, table_id: Optional[str], field_id: str
) -> bool:
    fields = await _db_fields(db, table_id)
    await ensure_permission_field_change_safe(
        db,
        normalize_table_id(table_id),
        field_id,
        deleting=True,
    )
    _ensure_field_can_be_deleted(fields, field_id)
    return await _delete_field(db, table_id, field_id)


async def delete_record(
    db: AsyncSession,
    table_id: Optional[str],
    record_id: str,
    user_id: Optional[int] = None,
    *,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> bool:
    resolved_table_id = normalize_table_id(table_id)
    return await _delete_record(
        db,
        resolved_table_id,
        record_id,
        user_id=user_id,
        actor_type=actor_type,
        source=source,
        trace_id=trace_id,
    )

# ---------------------------------------------------------------------------
# File backend write wrappers.
# ---------------------------------------------------------------------------

async def create_record_file(table_id: str) -> Dict[str, Any]:
    created = await _create_record_file(table_id)
    return _materialize_one(await _file_fields(table_id), created) or created


async def create_records_file(table_id: str, count: int) -> List[Dict[str, Any]]:
    created = await _create_records_file(table_id, count)
    return materialize_records(await _file_fields(table_id), created)


async def create_records_with_data_file(
    table_id: str, records_data: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    fields = await _file_fields(table_id)
    sanitized = [
        _normalize_text_payload(_strip_formula_values(dict(row), fields), fields)
        for row in records_data
    ]
    normalized = await normalize_relation_rows_file(fields, sanitized)
    created = await _create_records_with_data_file(table_id, normalized)
    return materialize_records(fields, created)


async def update_record_file(
    table_id: str,
    record_id: str,
    field_id_or_data: Any,
    value: Any = _MISSING,
) -> Optional[Dict[str, Any]]:
    """Update one file-backend field or a dict of fields."""
    fields = await _file_fields(table_id)
    if isinstance(field_id_or_data, dict) and value is _MISSING:
        sanitized = _normalize_text_payload(
            _strip_formula_values(dict(field_id_or_data), fields), fields
        )
        normalized = await normalize_relation_payload_file(fields, sanitized)
        patches = [
            {"recordId": record_id, "fieldId": field_id, "value": next_value}
            for field_id, next_value in normalized.items()
        ]
        if patches:
            await _apply_record_patches_file(table_id, patches)
        raw_store = await _get_full_store_for_table(table_id)
        raw_record = next(
            (row for row in raw_store.get("records", []) if row.get("id") == record_id),
            None,
        )
        return _materialize_one(fields, raw_record)

    field_id = str(field_id_or_data)
    _ensure_field_is_writable(fields, field_id)
    text_normalized = _normalize_text_payload({field_id: value}, fields)
    normalized = await normalize_relation_payload_file(fields, text_normalized)
    updated = await _update_record_file(
        table_id,
        record_id,
        field_id,
        normalized.get(field_id, text_normalized.get(field_id, value)),
    )
    return _materialize_one(fields, updated)


async def apply_record_patches_file(table_id: str, patches: List[Dict[str, Any]]) -> int:
    fields = await _file_fields(table_id)
    normalized_patches: List[Dict[str, Any]] = []
    for patch in patches:
        next_patch = dict(patch)
        field_id = patch.get("fieldId")
        if isinstance(field_id, str):
            _ensure_field_is_writable(fields, field_id)
            text_normalized = _normalize_text_payload(
                {field_id: patch.get("value")}, fields
            )
            normalized = await normalize_relation_payload_file(
                fields, text_normalized
            )
            next_patch["value"] = normalized.get(
                field_id, text_normalized.get(field_id, patch.get("value"))
            )
        normalized_patches.append(next_patch)
    return await _apply_record_patches_file(table_id, normalized_patches)


async def add_field_file(
    table_id: str, field_data: Dict[str, Any], index: Optional[int] = None
) -> Dict[str, Any]:
    fields = await _file_fields(table_id)
    _validate_field_add(fields, field_data)
    await validate_relation_field_file(field_data)
    return await _add_field_file(table_id, field_data, index)


async def update_field_file(
    table_id: str, field_id: str, updates: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    fields = await _file_fields(table_id)
    updated_field = _validate_field_update(fields, field_id, updates, ignore_none=True)
    if updated_field is not None:
        await validate_relation_field_file(updated_field)
    return await _update_field_file(table_id, field_id, updates)


async def delete_field_file(table_id: str, field_id: str) -> bool:
    fields = await _file_fields(table_id)
    _ensure_field_can_be_deleted(fields, field_id)
    return await _delete_field_file(table_id, field_id)


# Backward compatibility: expose functions at package level
__all__ = [
    "FormulaValidationError",
    "RelationValidationError",
    "AutoNumberValidationError",
    "resolve_table_id",
    "normalize_table_id",
    "table_file_exists",
    "init_table_file",
    "get_full_store_for_table",
    "create_record_file",
    "create_records_file",
    "create_records_with_data_file",
    "update_record_file",
    "delete_record_file",
    "apply_record_patches_file",
    "add_field_file",
    "update_field_file",
    "delete_field_file",
    "reorder_fields_file",
    "update_field_visibility_file",
    "update_filters_file",
    "clear_filters_file",
    "update_sorts_file",
    "clear_sorts_file",
    "update_group_config_file",
    "update_view_config_file",
    "create_view_file",
    "rename_view_file",
    "copy_view_file",
    "delete_view_file",
    "table_has_data",
    "init_table_db",
    "delete_table_data_db",
    "copy_table_db",
    "get_full_store",
    "create_record",
    "create_records",
    "create_records_with_data",
    "update_record",
    "update_record_fields",
    "delete_record",
    "apply_record_patches",
    "add_field",
    "update_field",
    "delete_field",
    "reorder_fields",
    "update_field_visibility",
    "get_filters",
    "add_filter",
    "update_filter",
    "delete_filter",
    "update_filters",
    "clear_filters",
    "get_sorts",
    "add_sort",
    "delete_sort",
    "update_sorts",
    "clear_sorts",
    "update_group_config",
    "update_view_config",
    "create_view",
    "rename_view",
    "copy_view",
    "delete_view",
]