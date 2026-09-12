"""Persistence-aware helpers for SmartTable link-to-record fields."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.services.relation_engine import (
    RelationValidationError,
    get_display_field_id,
    get_target_table_id,
    is_relation_field,
    normalize_relation_value,
    relation_record_ids,
    remove_linked_record,
    validate_relation_field_shape,
)
from app.services.smart_table_store.helpers import normalize_table_id, table_file_exists
from app.services.smart_table_store.file_backend import get_full_store_for_table as get_raw_file_store
from app.services.row_permissions import (
    allowed_record_ids,
    get_row_permission_policy,
    row_permission_restricts_user,
)
from app.services.workspace import (
    get_effective_permission_for_item,
    permission_allows,
)


def field_to_dict(field: TableField) -> Dict[str, Any]:
    return {
        "id": field.id,
        "tableId": field.table_id,
        "name": field.name,
        "type": field.type,
        "options": field.options,
        "property": field.property,
    }


async def validate_relation_field_db(
    db: AsyncSession, source_table_id: str | None, field: Mapping[str, Any]
) -> None:
    if not is_relation_field(field):
        return
    validate_relation_field_shape(field)
    source_id = normalize_table_id(source_table_id)
    target_id = get_target_table_id(field)

    result = await db.execute(
        select(WorkspaceItem).where(WorkspaceItem.id.in_([source_id, target_id]))
    )
    items = {item.id: item for item in result.scalars().all()}
    source_item = items.get(source_id)
    target_item = items.get(target_id)
    if not source_item or source_item.type != "table":
        raise RelationValidationError("Source table does not exist in workspace")
    if not target_item or target_item.type != "table":
        raise RelationValidationError("Relation target table does not exist")
    if source_item.workspace_id != target_item.workspace_id:
        raise RelationValidationError("Relation target table must be in the same workspace")

    display_field_id = get_display_field_id(field)
    if display_field_id:
        display_result = await db.execute(
            select(TableField).where(
                TableField.table_id == target_id,
                TableField.id == display_field_id,
            )
        )
        if display_result.scalars().first() is None:
            raise RelationValidationError(
                "Relation displayFieldId must reference a field in the target table"
            )

    # When this field already exists, the call comes from update_field. Validate
    # the new target/cardinality against current rows and migrate canonical
    # shape before the metadata update commits the transaction.
    field_id = str(field.get("id") or "")
    if field_id:
        existing_result = await db.execute(
            select(TableField.id).where(
                TableField.table_id == source_id,
                TableField.id == field_id,
            )
        )
        if existing_result.scalar() is not None:
            await normalize_existing_relation_field_db(db, source_id, field)


async def validate_relation_field_file(field: Mapping[str, Any]) -> None:
    if not is_relation_field(field):
        return
    validate_relation_field_shape(field)
    target_id = get_target_table_id(field)
    if not table_file_exists(target_id):
        raise RelationValidationError("Relation target file table does not exist")
    display_field_id = get_display_field_id(field)
    if display_field_id:
        target_store = await get_raw_file_store(target_id)
        if not any(item.get("id") == display_field_id for item in target_store.get("fields", [])):
            raise RelationValidationError(
                "Relation displayFieldId must reference a field in the target table"
            )


async def _ensure_target_record_ids_db(
    db: AsyncSession, target_table_id: str, record_ids: Sequence[str]
) -> None:
    unique_ids = list(dict.fromkeys(record_ids))
    if not unique_ids:
        return
    result = await db.execute(
        select(TableRecord.id).where(
            TableRecord.table_id == target_table_id,
            TableRecord.id.in_(unique_ids),
        )
    )
    existing = set(result.scalars().all())
    missing = [record_id for record_id in unique_ids if record_id not in existing]
    if missing:
        raise RelationValidationError(
            f"Linked record does not exist in target table: {missing[0]}"
        )


async def _ensure_target_record_ids_visible_db(
    db: AsyncSession,
    target_table_id: str,
    record_ids: Sequence[str],
    *,
    user_id: int | None,
) -> None:
    await _ensure_target_record_ids_db(db, target_table_id, record_ids)
    if user_id is None:
        return

    permission = await get_effective_permission_for_item(
        db,
        user_id,
        target_table_id,
    )
    if not permission_allows(permission, "read"):
        raise RelationValidationError("Linked record not found or no access")

    policy = await get_row_permission_policy(db, target_table_id)
    if not row_permission_restricts_user(policy, permission):
        return

    allowed = await allowed_record_ids(
        db,
        target_table_id,
        user_id=user_id,
        table_permission=permission,
        policy=policy,
    )
    hidden = [
        record_id
        for record_id in dict.fromkeys(record_ids)
        if str(record_id) not in allowed
    ]
    if hidden:
        raise RelationValidationError("Linked record not found or no access")


async def normalize_relation_payload_db(
    db: AsyncSession,
    fields: Sequence[Mapping[str, Any]],
    values: Mapping[str, Any],
    *,
    user_id: int | None = None,
) -> Dict[str, Any]:
    next_values = dict(values)
    field_map = {str(field.get("id")): field for field in fields if field.get("id")}
    grouped_ids: Dict[str, List[str]] = {}
    normalized_by_field: Dict[str, Any] = {}

    for field_id, value in values.items():
        field = field_map.get(field_id)
        if not field or not is_relation_field(field):
            continue
        normalized = normalize_relation_value(field, value)
        normalized_by_field[field_id] = normalized
        grouped_ids.setdefault(get_target_table_id(field), []).extend(
            relation_record_ids(field, normalized)
        )

    for target_table_id, record_ids in grouped_ids.items():
        await _ensure_target_record_ids_visible_db(
            db,
            target_table_id,
            record_ids,
            user_id=user_id,
        )

    next_values.update(normalized_by_field)
    return next_values


async def normalize_relation_rows_db(
    db: AsyncSession,
    fields: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    user_id: int | None = None,
) -> List[Dict[str, Any]]:
    normalized_rows: List[Dict[str, Any]] = []
    field_map = {str(field.get("id")): field for field in fields if field.get("id")}
    grouped_ids: Dict[str, List[str]] = {}

    for row in rows:
        next_row = dict(row)
        for field_id, value in row.items():
            field = field_map.get(field_id)
            if not field or not is_relation_field(field):
                continue
            normalized = normalize_relation_value(field, value)
            next_row[field_id] = normalized
            grouped_ids.setdefault(get_target_table_id(field), []).extend(
                relation_record_ids(field, normalized)
            )
        normalized_rows.append(next_row)

    for target_table_id, record_ids in grouped_ids.items():
        await _ensure_target_record_ids_visible_db(
            db,
            target_table_id,
            record_ids,
            user_id=user_id,
        )
    return normalized_rows


async def normalize_existing_relation_field_db(
    db: AsyncSession,
    source_table_id: str,
    field: Mapping[str, Any],
) -> int:
    """Validate a changed relation config against existing values and migrate shape."""
    if not is_relation_field(field):
        return 0
    field_id = str(field.get("id") or "")
    if not field_id:
        raise RelationValidationError("Relation field requires an id")

    result = await db.execute(
        select(TableRecord).where(TableRecord.table_id == source_table_id)
    )
    records = list(result.scalars().all())
    rows = [{field_id: (record.data or {}).get(field_id)} for record in records]
    normalized_rows = await normalize_relation_rows_db(db, [field], rows)

    changed = 0
    for record, normalized in zip(records, normalized_rows):
        previous = (record.data or {}).get(field_id)
        next_value = normalized.get(field_id)
        if previous == next_value:
            continue
        next_data = dict(record.data or {})
        next_data[field_id] = next_value
        record.data = next_data
        changed += 1
    return changed


async def normalize_relation_payload_file(
    fields: Sequence[Mapping[str, Any]], values: Mapping[str, Any]
) -> Dict[str, Any]:
    next_values = dict(values)
    field_map = {str(field.get("id")): field for field in fields if field.get("id")}
    target_stores: Dict[str, Dict[str, Any]] = {}

    for field_id, value in values.items():
        field = field_map.get(field_id)
        if not field or not is_relation_field(field):
            continue
        normalized = normalize_relation_value(field, value)
        target_id = get_target_table_id(field)
        if target_id not in target_stores:
            if not table_file_exists(target_id):
                raise RelationValidationError("Relation target file table does not exist")
            target_stores[target_id] = await get_raw_file_store(target_id)
        existing = {str(item.get("id")) for item in target_stores[target_id].get("records", [])}
        missing = [
            record_id
            for record_id in relation_record_ids(field, normalized)
            if record_id not in existing
        ]
        if missing:
            raise RelationValidationError(
                f"Linked record does not exist in target table: {missing[0]}"
            )
        next_values[field_id] = normalized
    return next_values


async def normalize_relation_rows_file(
    fields: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for row in rows:
        result.append(await normalize_relation_payload_file(fields, row))
    return result


async def cleanup_deleted_relation_references_db(
    db: AsyncSession,
    target_table_id: str,
    deleted_record_id: str,
    *,
    change_collector: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Remove reverse links while optionally collecting reversible ChangeItems.

    Deleting a target row is one logical action. Any relation rows modified as
    a side effect therefore increment their own record version and can be
    captured in the same ChangeSet as the target deletion.
    """
    target_result = await db.execute(
        select(WorkspaceItem).where(WorkspaceItem.id == target_table_id)
    )
    target_item = target_result.scalars().first()
    if not target_item:
        return 0

    table_result = await db.execute(
        select(WorkspaceItem.id).where(
            WorkspaceItem.workspace_id == target_item.workspace_id,
            WorkspaceItem.type == "table",
        )
    )
    table_ids = list(table_result.scalars().all())
    if not table_ids:
        return 0

    field_result = await db.execute(
        select(TableField).where(
            TableField.table_id.in_(table_ids),
            TableField.type == "relation",
        )
    )
    relation_fields = [
        field_to_dict(field)
        for field in field_result.scalars().all()
        if get_target_table_id(field_to_dict(field)) == target_table_id
    ]
    if not relation_fields:
        return 0

    fields_by_table: Dict[str, List[Dict[str, Any]]] = {}
    for field in relation_fields:
        owner_table_id = str(field.get("tableId") or "")
        if owner_table_id:
            fields_by_table.setdefault(owner_table_id, []).append(field)

    changed = 0
    for source_table_id, fields in fields_by_table.items():
        records_result = await db.execute(
            select(TableRecord)
            .where(TableRecord.table_id == source_table_id)
            .with_for_update()
        )
        for record in records_result.scalars().all():
            # The row being deleted does not need its own self-relation cleanup;
            # capturing it twice would make a single ChangeSet ambiguous.
            if source_table_id == target_table_id and record.id == deleted_record_id:
                continue
            before_data = dict(record.data or {})
            next_data = dict(before_data)
            changed_field_ids: List[str] = []
            for field in fields:
                field_id = str(field["id"])
                next_value, did_change = remove_linked_record(
                    field, next_data.get(field_id), deleted_record_id
                )
                if did_change:
                    next_data[field_id] = next_value
                    changed_field_ids.append(field_id)
            if not changed_field_ids:
                continue

            before_version = max(1, int(getattr(record, "version", 1) or 1))
            record.data = next_data
            record.version = before_version + 1
            changed += 1
            if change_collector is not None:
                meta = {
                    "orderIndex": int(record.order_index or 0),
                    "createdByUserId": record.created_by_user_id,
                }
                change_collector.append(
                    {
                        "table_id": source_table_id,
                        "entity_type": "record",
                        "entity_id": record.id,
                        "before_data": before_data,
                        "after_data": dict(next_data),
                        "before_meta": dict(meta),
                        "after_meta": dict(meta),
                        "version_before": before_version,
                        "version_after": record.version,
                        "changed_fields": sorted(set(changed_field_ids)),
                    }
                )
    return changed


async def cleanup_deleted_relation_references_file(
    target_table_id: str, deleted_record_id: str
) -> int:
    """File tables do not have a workspace reverse index; DB mode provides cleanup."""
    return 0
