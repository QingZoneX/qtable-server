"""
Database backend operations for smart table store
"""
from typing import Any, Dict, List, Optional
import json
import time
import uuid

from sqlalchemy import select, delete, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.smart_table import (
    TableField,
    TableRecord,
    TableView,
    TableFilter,
    TableSort,
    TableGroup,
    TableRowPermissionPolicy,
    WorkspaceItem,
)

from app.services.auto_number_engine import (
    allocate_auto_number_values,
    is_auto_number_field,
    normalize_auto_number_property,
)
from app.models.change_history import ChangeItem, ChangeSet, RecycleBinRecord
from app.services.change_history import (
    append_change_set,
    changed_fields,
    record_meta,
    record_version,
)
from app.services.member_field import (
    ensure_member_field_change_safe,
    normalize_member_field_definition,
    normalize_member_value_for_field,
    normalize_member_values_in_payloads,
)


def _new_record_id() -> str:
    """Return a collision-safe record id independent of wall-clock timing."""
    return f"r{uuid.uuid4().hex}"


async def table_has_data(db: AsyncSession, table_id: str) -> bool:
    """Return whether the table already has at least one field."""
    result = await db.execute(
        select(TableField).where(TableField.table_id == table_id).limit(1)
    )
    return result.scalars().first() is not None


def _fallback_core_schema() -> Dict[str, Any]:
    """Minimal schema used only when a template is missing or malformed."""
    return {
        "fields": [{"id": "f1", "name": "文本", "type": "text"}],
        "views": [
            {
                "id": "v1",
                "name": "表格视图",
                "type": "grid",
                "config": {
                    "toolbar": {
                        "items": [
                            "insertRow",
                            "fields",
                            "filter",
                            "group",
                            "sort",
                            "automations",
                            "share",
                        ]
                    },
                    "filters": [],
                    "sorts": [],
                    "groupConfig": {"fieldId": None, "order": "asc"},
                    "hiddenFieldIds": [],
                },
            }
        ],
        "filters": [],
        "sorts": [],
        "groupConfig": {"fieldId": None, "order": "asc"},
    }


async def init_table_db(
    db: AsyncSession,
    table_id: str,
    template_id: Optional[str] = None,
    template_snapshot: Optional[Dict[str, Any]] = None,
) -> bool:
    """Initialize or repair a table schema from a template.

    Fresh tables receive the complete requested template. Partially initialized
    tables are repaired conservatively: an entirely missing field set and/or
    view set is restored without overwriting existing custom schema.

    This makes initialization idempotent, which is important because an older
    create-table flow could leave a WorkspaceItem committed while template
    initialization failed.
    """
    from .helpers import _load_table_json, generate_table_from_template, DATA_DIR
    import os

    if template_snapshot is not None:
        # Template catalog snapshots are already validated and portable tokens
        # have been resolved for this destination table.
        store = json.loads(json.dumps(template_snapshot))
    elif os.path.exists(os.path.join(DATA_DIR, f"{table_id}.json")):
        store = _load_table_json(table_id)
    else:
        store = generate_table_from_template(template_id)

    fallback = _fallback_core_schema()
    template_fields = store.get("fields") or fallback["fields"]
    template_views = store.get("views") or fallback["views"]

    fields_result = await db.execute(
        select(TableField)
        .where(TableField.table_id == table_id)
        .order_by(TableField.order_index)
    )
    existing_fields = fields_result.scalars().all()

    views_result = await db.execute(
        select(TableView).where(TableView.table_id == table_id)
    )
    existing_views = views_result.scalars().all()

    records_result = await db.execute(
        select(TableRecord)
        .where(TableRecord.table_id == table_id)
        .order_by(TableRecord.order_index)
        .with_for_update()
    )
    existing_records = records_result.scalars().all()

    filters_result = await db.execute(
        select(TableFilter).where(TableFilter.table_id == table_id).limit(1)
    )
    has_filters = filters_result.scalars().first() is not None

    sorts_result = await db.execute(
        select(TableSort).where(TableSort.table_id == table_id).limit(1)
    )
    has_sorts = sorts_result.scalars().first() is not None

    group_result = await db.execute(
        select(TableGroup).where(TableGroup.table_id == table_id).limit(1)
    )
    has_group = group_result.scalars().first() is not None

    # A table with no table-specific structure at all is a genuinely fresh
    # table. Only fresh tables inherit template filters/sorts/group settings.
    is_fresh = not (
        existing_fields
        or existing_views
        or existing_records
        or has_filters
        or has_sorts
        or has_group
    )
    changed = False

    if not existing_fields:
        for idx, field_data in enumerate(template_fields):
            field_id = str(field_data.get("id") or f"f{idx + 1}")
            db.add(
                TableField(
                    id=field_id,
                    table_id=table_id,
                    name=str(field_data.get("name") or "文本"),
                    type=str(field_data.get("type") or "text"),
                    options=field_data.get("options"),
                    property=field_data.get("property"),
                    order_index=idx,
                )
            )

        # Rows created while the table had zero fields used to contain only an
        # id. Backfill the repaired schema so old invisible rows become normal
        # editable rows instead of being discarded.
        for record in existing_records:
            next_data = dict(record.data or {})
            for idx, field_data in enumerate(template_fields):
                field_id = str(field_data.get("id") or f"f{idx + 1}")
                next_data.setdefault(field_id, None)
            record.data = next_data
        changed = True

    if not existing_views:
        for view_data in template_views:
            view_id = str(view_data.get("id") or "v1")
            db.add(
                TableView(
                    id=view_id,
                    table_id=table_id,
                    name=str(view_data.get("name") or "表格视图"),
                    type=str(view_data.get("type") or "grid"),
                    config=view_data.get("config"),
                )
            )
        changed = True

    if is_fresh:
        # Example records are optional template content. They are inserted only
        # for a genuinely fresh table and receive destination-safe IDs before
        # this function is called by the template catalog.
        for idx, record_data in enumerate(store.get("records") or []):
            if not isinstance(record_data, dict):
                continue
            record_id = str(record_data.get("id") or _new_record_id())
            record_content = {
                key: value
                for key, value in record_data.items()
                if key != "id"
            }
            db.add(
                TableRecord(
                    id=record_id,
                    table_id=table_id,
                    data=record_content,
                    order_index=idx,
                    created_by_user_id=None,
                )
            )
            changed = True

        for idx, filter_data in enumerate(store.get("filters") or []):
            db.add(
                TableFilter(
                    id=str(filter_data.get("id") or f"flt_{idx + 1}"),
                    table_id=table_id,
                    field_id=filter_data["fieldId"],
                    operator=filter_data["operator"],
                    value=filter_data.get("value"),
                    logic=filter_data.get("logic", "and"),
                    order_index=idx,
                )
            )
            changed = True

        for idx, sort_data in enumerate(store.get("sorts") or []):
            db.add(
                TableSort(
                    id=str(
                        sort_data.get("id")
                        or f"s{int(time.time() * 1000)}_{idx}"
                    ),
                    table_id=table_id,
                    field_id=sort_data["fieldId"],
                    order=sort_data["order"],
                    order_index=idx,
                )
            )
            changed = True

        group_config = store.get("groupConfig")
        if group_config:
            db.add(
                TableGroup(
                    id=f"grp_{table_id}",
                    table_id=table_id,
                    field_id=group_config.get("fieldId"),
                    order=group_config.get("order", "asc"),
                )
            )
            changed = True

    if changed:
        await db.commit()
    return changed


async def _get_record_creation_fields(
    db: AsyncSession,
    table_id: str,
) -> List[TableField]:
    """Load writable row schema, repairing legacy zero-field tables if needed."""
    result = await db.execute(
        select(TableField)
        .where(TableField.table_id == table_id)
        .order_by(TableField.order_index)
        .with_for_update()
    )
    fields = result.scalars().all()
    if fields:
        return fields

    await init_table_db(db, table_id, "blank")
    result = await db.execute(
        select(TableField)
        .where(TableField.table_id == table_id)
        .order_by(TableField.order_index)
        .with_for_update()
    )
    return result.scalars().all()


async def delete_table_data_db(db: AsyncSession, table_id: str) -> None:
    """Delete all table data from database"""
    await db.execute(delete(TableField).where(TableField.table_id == table_id))
    await db.execute(delete(TableRecord).where(TableRecord.table_id == table_id))
    await db.execute(delete(TableView).where(TableView.table_id == table_id))
    await db.execute(delete(TableFilter).where(TableFilter.table_id == table_id))
    await db.execute(delete(TableSort).where(TableSort.table_id == table_id))
    await db.execute(delete(TableGroup).where(TableGroup.table_id == table_id))
    await db.execute(delete(TableRowPermissionPolicy).where(TableRowPermissionPolicy.table_id == table_id))
    # A hard table deletion must not leave recoverable row snapshots behind.
    # Delete items by both entity table and owner ChangeSet so cross-table
    # relation-cleanup entries cannot become orphaned audit rows.
    change_set_result = await db.execute(
        select(ChangeSet.id).where(ChangeSet.table_id == table_id)
    )
    owned_change_set_ids = list(change_set_result.scalars().all())
    if owned_change_set_ids:
        await db.execute(
            delete(ChangeItem).where(
                ChangeItem.change_set_id.in_(owned_change_set_ids)
            )
        )
    await db.execute(delete(ChangeItem).where(ChangeItem.table_id == table_id))
    await db.execute(delete(RecycleBinRecord).where(RecycleBinRecord.table_id == table_id))
    await db.execute(delete(ChangeSet).where(ChangeSet.table_id == table_id))
    await db.commit()


async def copy_table_db(
    db: AsyncSession, source_table_id: str, target_table_id: str
) -> None:
    """Copy table data from source to target in database"""
    source_fields = await db.execute(
        select(TableField).where(TableField.table_id == source_table_id).order_by(TableField.order_index)
    )
    source_records = await db.execute(
        select(TableRecord).where(TableRecord.table_id == source_table_id).order_by(TableRecord.order_index)
    )
    source_views = await db.execute(
        select(TableView).where(TableView.table_id == source_table_id)
    )
    source_filters = await db.execute(
        select(TableFilter).where(TableFilter.table_id == source_table_id).order_by(TableFilter.order_index)
    )
    source_sorts = await db.execute(
        select(TableSort).where(TableSort.table_id == source_table_id).order_by(TableSort.order_index)
    )
    source_group = await db.execute(
        select(TableGroup).where(TableGroup.table_id == source_table_id).limit(1)
    )
    source_row_policy = await db.execute(
        select(TableRowPermissionPolicy)
        .where(TableRowPermissionPolicy.table_id == source_table_id)
        .limit(1)
    )

    for f in source_fields.scalars().all():
        db.add(
            TableField(
                id=f.id,
                table_id=target_table_id,
                name=f.name,
                type=f.type,
                options=f.options,
                property=f.property,
                order_index=f.order_index,
            )
        )

    for r in source_records.scalars().all():
        db.add(
            TableRecord(
                id=r.id,
                table_id=target_table_id,
                data=r.data,
                order_index=r.order_index,
                created_by_user_id=r.created_by_user_id,
            )
        )

    for v in source_views.scalars().all():
        db.add(
            TableView(
                id=v.id,
                table_id=target_table_id,
                name=v.name,
                type=v.type,
                config=v.config,
            )
        )

    for f in source_filters.scalars().all():
        db.add(
            TableFilter(
                id=f.id,
                table_id=target_table_id,
                field_id=f.field_id,
                operator=f.operator,
                value=f.value,
                logic=f.logic,
                order_index=f.order_index,
            )
        )

    for s in source_sorts.scalars().all():
        db.add(
            TableSort(
                id=s.id,
                table_id=target_table_id,
                field_id=s.field_id,
                order=s.order,
                order_index=s.order_index,
            )
        )

    group = source_group.scalars().first()
    if group:
        db.add(
            TableGroup(
                id=f"grp_{target_table_id}",
                table_id=target_table_id,
                field_id=group.field_id,
                order=group.order,
            )
        )

    row_policy = source_row_policy.scalars().first()
    if row_policy:
        db.add(
            TableRowPermissionPolicy(
                table_id=target_table_id,
                mode=row_policy.mode,
                member_field_id=row_policy.member_field_id,
            )
        )

    await db.commit()


async def get_full_store(db: AsyncSession, table_id: Optional[str] = None) -> Dict[str, Any]:
    """Get full table store from database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    
    # Fetch Fields
    result_fields = await db.execute(
        select(TableField).where(TableField.table_id == table_id).order_by(TableField.order_index)
    )
    fields = result_fields.scalars().all()
    fields_data = [
        {
            "id": f.id,
            "name": f.name,
            "type": f.type,
            "options": f.options,
            "property": f.property,
        }
        for f in fields
    ]

    # Extract hidden field IDs from property.hidden
    hidden_field_ids = [
        f.id for f in fields if f.property and f.property.get("hidden", False)
    ]

    # Fetch Records
    result_records = await db.execute(
        select(TableRecord).where(TableRecord.table_id == table_id).order_by(TableRecord.order_index)
    )
    records = result_records.scalars().all()
    records_data = []
    for r in records:
        rec = r.data.copy()
        rec["id"] = r.id
        records_data.append(rec)

    # Fetch Views
    result_views = await db.execute(select(TableView).where(TableView.table_id == table_id))
    views = result_views.scalars().all()
    views_data = [
        {
            "id": v.id,
            "name": v.name,
            "type": v.type,
            "config": v.config,
        }
        for v in views
    ]

    # Fetch Filters
    result_filters = await db.execute(
        select(TableFilter).where(TableFilter.table_id == table_id).order_by(TableFilter.order_index)
    )
    filters = result_filters.scalars().all()
    filters_data = [
        {
            "id": f.id,
            "fieldId": f.field_id,
            "operator": f.operator,
            "value": f.value,
            "logic": f.logic,
        }
        for f in filters
    ]

    # Fetch Sorts
    result_sorts = await db.execute(
        select(TableSort).where(TableSort.table_id == table_id).order_by(TableSort.order_index)
    )
    sorts = result_sorts.scalars().all()
    sorts_data = [
        {
            "fieldId": s.field_id,
            "order": s.order,
        }
        for s in sorts
    ]

    # Fetch Group Config
    result_group = await db.execute(
        select(TableGroup).where(TableGroup.table_id == table_id).limit(1)
    )
    group_config = result_group.scalars().first()
    group_config_data = {
        "fieldId": group_config.field_id if group_config else None,
        "order": group_config.order if group_config else "asc"
    }

    return {
        "fields": fields_data,
        "records": records_data,
        "views": views_data,
        "hiddenFieldIds": hidden_field_ids,
        "filters": filters_data,
        "sorts": sorts_data,
        "groupConfig": group_config_data,
    }


async def create_record(
    db: AsyncSession,
    table_id: Optional[str] = None,
    created_by_user_id: Optional[int] = None,
    *,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create one record and its audit entry in the same transaction."""
    from .helpers import normalize_table_id

    table_id = normalize_table_id(table_id)
    fields = await _get_record_creation_fields(db, table_id)

    record_id = _new_record_id()
    data: Dict[str, Any] = {}
    for field in fields:
        if is_auto_number_field({"type": field.type}):
            values, next_property = allocate_auto_number_values(field.property, 1)
            data[field.id] = values[0]
            field.property = next_property
        else:
            data[field.id] = None

    result_max = await db.execute(
        select(func.max(TableRecord.order_index)).where(TableRecord.table_id == table_id)
    )
    max_order = result_max.scalar() or 0

    new_record = TableRecord(
        id=record_id,
        table_id=table_id,
        data=data,
        order_index=max_order + 1,
        created_by_user_id=created_by_user_id,
        version=1,
    )
    db.add(new_record)
    await append_change_set(
        db,
        table_id=table_id,
        actor_id=created_by_user_id,
        actor_type=actor_type,
        operation="create",
        source=source,
        trace_id=trace_id,
        summary=f"Create record {record_id}",
        items=[
            {
                "table_id": table_id,
                "entity_type": "record",
                "entity_id": record_id,
                "before_data": None,
                "after_data": dict(data),
                "before_meta": None,
                "after_meta": record_meta(new_record),
                "version_before": None,
                "version_after": 1,
                "changed_fields": changed_fields(None, data),
            }
        ],
    )
    await db.commit()
    await db.refresh(new_record)

    res = dict(new_record.data or {})
    res["id"] = new_record.id
    return res

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
    """Create multiple records as one logical ChangeSet."""
    from .helpers import normalize_table_id

    table_id = normalize_table_id(table_id)
    amount = max(1, min(999, int(count)))
    fields = await _get_record_creation_fields(db, table_id)
    auto_values: Dict[str, List[int]] = {}
    for field in fields:
        if is_auto_number_field({"type": field.type}):
            values, next_property = allocate_auto_number_values(field.property, amount)
            auto_values[field.id] = values
            field.property = next_property

    result_max = await db.execute(
        select(func.max(TableRecord.order_index)).where(TableRecord.table_id == table_id)
    )
    max_order = result_max.scalar() or 0
    created: List[Dict[str, Any]] = []
    history_items: List[Dict[str, Any]] = []
    for i in range(amount):
        record_id = _new_record_id()
        data: Dict[str, Any] = {}
        for field in fields:
            data[field.id] = auto_values[field.id][i] if field.id in auto_values else None
        record = TableRecord(
            id=record_id,
            table_id=table_id,
            data=data,
            order_index=max_order + i + 1,
            created_by_user_id=created_by_user_id,
            version=1,
        )
        db.add(record)
        created.append({**data, "id": record_id})
        history_items.append(
            {
                "table_id": table_id,
                "entity_type": "record",
                "entity_id": record_id,
                "before_data": None,
                "after_data": dict(data),
                "before_meta": None,
                "after_meta": record_meta(record),
                "version_before": None,
                "version_after": 1,
                "changed_fields": changed_fields(None, data),
            }
        )
    await append_change_set(
        db,
        table_id=table_id,
        actor_id=created_by_user_id,
        actor_type=actor_type,
        operation="create",
        source=source,
        trace_id=trace_id,
        summary=f"Create {amount} records",
        items=history_items,
    )
    await db.commit()
    return created

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
    """Batch create rows and capture the whole batch in one ChangeSet."""
    from .helpers import normalize_table_id

    table_id = normalize_table_id(table_id)
    amount = len(records_data)
    fields = await _get_record_creation_fields(db, table_id)
    records_data = await normalize_member_values_in_payloads(
        db,
        table_id,
        records_data,
    )
    auto_values: Dict[str, List[int]] = {}
    for field in fields:
        if is_auto_number_field({"type": field.type}):
            values, next_property = allocate_auto_number_values(field.property, amount)
            auto_values[field.id] = values
            field.property = next_property

    result_max = await db.execute(
        select(func.max(TableRecord.order_index)).where(TableRecord.table_id == table_id)
    )
    max_order = result_max.scalar() or 0

    created: List[Dict[str, Any]] = []
    history_items: List[Dict[str, Any]] = []
    for i, row_data in enumerate(records_data):
        record_id = _new_record_id()
        data: Dict[str, Any] = {}
        for field in fields:
            if field.id in auto_values:
                data[field.id] = auto_values[field.id][i]
            else:
                data[field.id] = row_data.get(field.id) if field.id in row_data else None
        record = TableRecord(
            id=record_id,
            table_id=table_id,
            data=data,
            order_index=max_order + i + 1,
            created_by_user_id=created_by_user_id,
            version=1,
        )
        db.add(record)
        created.append({"id": record_id, **data})
        history_items.append(
            {
                "table_id": table_id,
                "entity_type": "record",
                "entity_id": record_id,
                "before_data": None,
                "after_data": dict(data),
                "before_meta": None,
                "after_meta": record_meta(record),
                "version_before": None,
                "version_after": 1,
                "changed_fields": changed_fields(None, data),
            }
        )

    if history_items:
        await append_change_set(
            db,
            table_id=table_id,
            actor_id=created_by_user_id,
            actor_type=actor_type,
            operation="create",
            source=source,
            trace_id=trace_id,
            summary=f"Create {len(history_items)} records with data",
            items=history_items,
        )
    await db.commit()
    return created

async def update_record(
    db: AsyncSession,
    table_id: Optional[str],
    record_id: str,
    field_id: str,
    value: Any,
    *,
    user_id: Optional[int] = None,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update one field with optimistic versioning and history."""
    from .helpers import normalize_table_id

    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableRecord)
        .where(TableRecord.id == record_id, TableRecord.table_id == table_id)
        .with_for_update()
    )
    record = result.scalars().first()
    if not record:
        return None

    value = await normalize_member_value_for_field(
        db,
        table_id,
        field_id,
        value,
    )
    before_data = dict(record.data or {})
    if before_data.get(field_id) == value:
        res = dict(before_data)
        res["id"] = record.id
        return res

    before_meta = record_meta(record)
    before_version = record_version(record)
    new_data = dict(before_data)
    new_data[field_id] = value
    record.data = new_data
    record.version = before_version + 1

    await append_change_set(
        db,
        table_id=table_id,
        actor_id=user_id,
        actor_type=actor_type,
        operation="update",
        source=source,
        trace_id=trace_id,
        summary=f"Update record {record_id}",
        items=[
            {
                "table_id": table_id,
                "entity_type": "record",
                "entity_id": record_id,
                "before_data": before_data,
                "after_data": dict(new_data),
                "before_meta": before_meta,
                "after_meta": record_meta(record),
                "version_before": before_version,
                "version_after": record.version,
                "changed_fields": [field_id],
            }
        ],
    )
    await db.commit()
    await db.refresh(record)

    res = dict(record.data or {})
    res["id"] = record.id
    return res

async def update_record_fields(
    db: AsyncSession,
    table_id: Optional[str],
    record_id: str,
    fields_data: Dict[str, Any],
    *,
    user_id: Optional[int] = None,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically update several fields as one ChangeSet."""
    from .helpers import normalize_table_id

    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableRecord)
        .where(TableRecord.id == record_id, TableRecord.table_id == table_id)
        .with_for_update()
    )
    record = result.scalars().first()
    if not record:
        return None

    normalized_fields_data = (
        await normalize_member_values_in_payloads(
            db,
            table_id,
            [fields_data],
        )
    )[0]
    before_data = dict(record.data or {})
    new_data = dict(before_data)
    for field_id, value in normalized_fields_data.items():
        if value is not None:
            new_data[field_id] = value

    changed = changed_fields(before_data, new_data)
    if not changed:
        res = dict(before_data)
        res["id"] = record.id
        return res

    before_meta = record_meta(record)
    before_version = record_version(record)
    record.data = new_data
    record.version = before_version + 1
    await append_change_set(
        db,
        table_id=table_id,
        actor_id=user_id,
        actor_type=actor_type,
        operation="update",
        source=source,
        trace_id=trace_id,
        summary=f"Update {len(changed)} fields on record {record_id}",
        items=[
            {
                "table_id": table_id,
                "entity_type": "record",
                "entity_id": record_id,
                "before_data": before_data,
                "after_data": dict(new_data),
                "before_meta": before_meta,
                "after_meta": record_meta(record),
                "version_before": before_version,
                "version_after": record.version,
                "changed_fields": changed,
            }
        ],
    )
    await db.commit()
    await db.refresh(record)

    res = dict(record.data or {})
    res["id"] = record.id
    return res

async def apply_record_patches(
    db: AsyncSession,
    table_id: Optional[str],
    patches: List[Dict[str, Any]],
    *,
    user_id: Optional[int] = None,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> int:
    """Apply a patch batch and persist it as one logical ChangeSet."""
    from .helpers import normalize_table_id

    table_id = normalize_table_id(table_id)
    normalized_values = await normalize_member_values_in_payloads(
        db,
        table_id,
        [
            {str(patch["fieldId"]): patch.get("value")}
            for patch in patches
        ],
    )
    patches = [
        {
            **patch,
            "value": normalized.get(str(patch["fieldId"])),
        }
        for patch, normalized in zip(patches, normalized_values)
    ]
    record_ids = list(dict.fromkeys(str(patch["recordId"]) for patch in patches))
    if not record_ids:
        return 0
    result = await db.execute(
        select(TableRecord)
        .where(
            TableRecord.id.in_(record_ids),
            TableRecord.table_id == table_id,
        )
        .with_for_update()
    )
    records = result.scalars().all()
    record_map = {record.id: record for record in records}
    patches_by_record: Dict[str, List[Dict[str, Any]]] = {}
    for patch in patches:
        patches_by_record.setdefault(str(patch["recordId"]), []).append(patch)

    history_items: List[Dict[str, Any]] = []
    for record_id in record_ids:
        record = record_map.get(record_id)
        if not record:
            continue
        before_data = dict(record.data or {})
        new_data = dict(before_data)
        for patch in patches_by_record.get(record_id, []):
            new_data[str(patch["fieldId"])] = patch.get("value")
        changed = changed_fields(before_data, new_data)
        if not changed:
            continue
        before_meta = record_meta(record)
        before_version = record_version(record)
        record.data = new_data
        record.version = before_version + 1
        history_items.append(
            {
                "table_id": table_id,
                "entity_type": "record",
                "entity_id": record.id,
                "before_data": before_data,
                "after_data": dict(new_data),
                "before_meta": before_meta,
                "after_meta": record_meta(record),
                "version_before": before_version,
                "version_after": record.version,
                "changed_fields": changed,
            }
        )

    if history_items:
        await append_change_set(
            db,
            table_id=table_id,
            actor_id=user_id,
            actor_type=actor_type,
            operation="batch_update",
            source=source,
            trace_id=trace_id,
            summary=f"Update {len(history_items)} records",
            items=history_items,
        )
    await db.commit()
    return len(records)

async def add_field(
    db: AsyncSession,
    table_id: Optional[str],
    field_data: Dict[str, Any],
    index: Optional[int] = None,
) -> Dict[str, Any]:
    """Add a field; auto-number fields backfill existing records once."""
    from .helpers import normalize_table_id

    table_id = normalize_table_id(table_id)
    field_data = normalize_member_field_definition(field_data)
    result = await db.execute(
        select(TableField).where(TableField.table_id == table_id).order_by(TableField.order_index)
    )
    existing_fields = result.scalars().all()

    if index is not None and 0 <= index <= len(existing_fields):
        for i, field in enumerate(existing_fields):
            if i >= index:
                field.order_index += 1
        new_order_index = index
    else:
        new_order_index = len(existing_fields)

    property_value = field_data.get("property")
    if is_auto_number_field(field_data):
        property_value = normalize_auto_number_property(property_value)
        # nextNumber is server-owned. Ignore any client-supplied counter.
        property_value["nextNumber"] = property_value["start"]

    new_field = TableField(
        id=field_data["id"],
        table_id=table_id,
        name=field_data["name"],
        type=field_data["type"],
        options=field_data.get("options"),
        property=property_value,
        order_index=new_order_index,
    )
    db.add(new_field)

    if is_auto_number_field(field_data):
        result_records = await db.execute(
            select(TableRecord)
            .where(TableRecord.table_id == table_id)
            .order_by(TableRecord.order_index, TableRecord.id)
            .with_for_update()
        )
        records = result_records.scalars().all()
        values, next_property = allocate_auto_number_values(property_value, len(records))
        for record, number in zip(records, values):
            next_data = dict(record.data or {})
            next_data[field_data["id"]] = number
            record.data = next_data
        new_field.property = next_property
        field_data = {**field_data, "property": next_property}

    await db.commit()
    return field_data

async def update_field(
    db: AsyncSession, table_id: Optional[str], field_id: str, updates: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Update field metadata, preserving the auto-number sequence counter."""
    from .helpers import normalize_table_id

    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableField)
        .where(TableField.id == field_id, TableField.table_id == table_id)
        .with_for_update()
    )
    field = result.scalars().first()
    if not field:
        return None

    updates = dict(updates)
    old_type = field.type
    next_type = updates.get("type", field.type)
    next_property = updates.get("property", field.property)

    if next_type == "member":
        normalized_member = normalize_member_field_definition(
            {
                "type": "member",
                "options": updates.get("options", field.options),
                "property": next_property,
            }
        )
        next_property = normalized_member["property"]
        updates["options"] = None
        updates["property"] = next_property
        await ensure_member_field_change_safe(
            db,
            table_id,
            field_id,
            old_type=old_type,
            next_type=next_type,
            next_property=next_property,
        )

    if next_type == "autoNumber":
        if old_type == "autoNumber":
            current_property = normalize_auto_number_property(field.property)
            requested_property = dict(next_property or {})
            requested_property["start"] = current_property["start"]
            next_property = normalize_auto_number_property(
                requested_property,
                preserve_next=int(current_property["nextNumber"]),
            )
        else:
            next_property = normalize_auto_number_property(next_property)
            # Client cannot seed the internal counter directly.
            next_property["nextNumber"] = next_property["start"]
            result_records = await db.execute(
                select(TableRecord)
                .where(TableRecord.table_id == table_id)
                .order_by(TableRecord.order_index, TableRecord.id)
                .with_for_update()
            )
            records = result_records.scalars().all()
            values, next_property = allocate_auto_number_values(
                next_property, len(records)
            )
            for record, number in zip(records, values):
                next_data = dict(record.data or {})
                next_data[field_id] = number
                record.data = next_data

    if "name" in updates:
        field.name = updates["name"]
    if "type" in updates:
        field.type = next_type
    if "options" in updates:
        field.options = updates["options"]
    if "property" in updates or next_type == "autoNumber":
        field.property = next_property

    await db.commit()

    return {
        "id": field.id,
        "name": field.name,
        "type": field.type,
        "options": field.options,
        "property": field.property,
    }

async def delete_field(
    db: AsyncSession, table_id: Optional[str], field_id: str
) -> bool:
    """Delete a field in database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableField).where(TableField.id == field_id, TableField.table_id == table_id)
    )
    field = result.scalars().first()
    if not field:
        return False
    
    await db.delete(field)
    
    # Clean up records
    # We need to remove the field key from all records' data JSON
    result_records = await db.execute(
        select(TableRecord).where(TableRecord.table_id == table_id)
    )
    records = result_records.scalars().all()
    for rec in records:
        if field_id in rec.data:
            new_data = rec.data.copy()
            new_data.pop(field_id)
            rec.data = new_data
            
    await db.commit()
    return True


async def delete_record(
    db: AsyncSession,
    table_id: Optional[str],
    record_id: str,
    *,
    user_id: Optional[int] = None,
    actor_type: str = "user",
    source: str = "smart_table",
    trace_id: Optional[str] = None,
) -> bool:
    """Move a record to the recycle bin and capture reversible side effects."""
    from .helpers import normalize_table_id
    from app.services.relation_store import cleanup_deleted_relation_references_db

    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableRecord)
        .where(TableRecord.id == record_id, TableRecord.table_id == table_id)
        .with_for_update()
    )
    record = result.scalars().first()
    if not record:
        return False

    # Preserve current behavior: deleting a relation target removes reverse
    # links. Those linked-row updates are captured in this same ChangeSet.
    related_changes: List[Dict[str, Any]] = []
    await cleanup_deleted_relation_references_db(
        db,
        table_id,
        record_id,
        change_collector=related_changes,
    )

    before_data = dict(record.data or {})
    before_meta = record_meta(record)
    before_version = record_version(record)
    deleted_version = before_version + 1
    target_item = {
        "table_id": table_id,
        "entity_type": "record",
        "entity_id": record_id,
        "before_data": before_data,
        "after_data": None,
        "before_meta": before_meta,
        "after_meta": None,
        "version_before": before_version,
        "version_after": deleted_version,
        "changed_fields": changed_fields(before_data, None),
    }
    change_set = await append_change_set(
        db,
        table_id=table_id,
        actor_id=user_id,
        actor_type=actor_type,
        operation="delete",
        source=source,
        trace_id=trace_id,
        summary=f"Delete record {record_id}",
        items=[target_item, *related_changes],
    )
    db.add(
        RecycleBinRecord(
            id=f"rcy_{uuid.uuid4().hex}",
            table_id=table_id,
            record_id=record_id,
            data=before_data,
            order_index=int(record.order_index or 0),
            created_by_user_id=record.created_by_user_id,
            record_version=deleted_version,
            deleted_by_user_id=user_id,
            change_set_id=change_set.id,
            status="deleted",
        )
    )
    await db.delete(record)
    await db.commit()
    return True

async def reorder_fields(
    db: AsyncSession, table_id: Optional[str], field_order: List[str]
) -> bool:
    """Update the order_index of fields based on the provided order"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    for idx, field_id in enumerate(field_order):
        result = await db.execute(
            select(TableField).where(TableField.id == field_id, TableField.table_id == table_id)
        )
        field = result.scalars().first()
        if field:
            field.order_index = idx

    await db.commit()
    return True


async def update_field_visibility(
    db: AsyncSession, table_id: Optional[str], hidden_field_ids: List[str]
) -> bool:
    """Update field visibility by storing hidden status in property JSON"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    # First, get all fields
    result = await db.execute(select(TableField).where(TableField.table_id == table_id))
    fields = result.scalars().all()

    # Update property.hidden for all fields
    for field in fields:
        # Get current property and create a new dict to ensure SQLAlchemy detects the change
        current_property = dict(field.property) if field.property else {}
        current_property["hidden"] = field.id in hidden_field_ids
        field.property = current_property

    await db.commit()
    return True


async def get_filters(
    db: AsyncSession, table_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Get all filters from the database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableFilter).where(TableFilter.table_id == table_id).order_by(TableFilter.order_index)
    )
    filters = result.scalars().all()
    return [
        {
            "id": f.id,
            "fieldId": f.field_id,
            "operator": f.operator,
            "value": f.value,
            "logic": f.logic,
        }
        for f in filters
    ]


async def add_filter(
    db: AsyncSession, table_id: Optional[str], filter_data: Dict[str, Any]
) -> Dict[str, Any]:
    """Add a new filter to the database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    # Get max order
    result_max = await db.execute(
        select(func.max(TableFilter.order_index)).where(TableFilter.table_id == table_id)
    )
    max_order = result_max.scalar() or 0

    new_filter = TableFilter(
        id=filter_data["id"],
        table_id=table_id,
        field_id=filter_data["fieldId"],
        operator=filter_data["operator"],
        value=filter_data.get("value"),
        logic=filter_data.get("logic", "and"),
        order_index=max_order + 1,
    )
    db.add(new_filter)
    await db.commit()
    return filter_data


async def update_filter(
    db: AsyncSession, table_id: Optional[str], filter_id: str, updates: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Update an existing filter"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableFilter).where(TableFilter.id == filter_id, TableFilter.table_id == table_id)
    )
    filter_obj = result.scalars().first()
    if not filter_obj:
        return None

    if "fieldId" in updates:
        filter_obj.field_id = updates["fieldId"]
    if "operator" in updates:
        filter_obj.operator = updates["operator"]
    if "value" in updates:
        filter_obj.value = updates["value"]
    if "logic" in updates:
        filter_obj.logic = updates["logic"]

    await db.commit()
    return {
        "id": filter_obj.id,
        "fieldId": filter_obj.field_id,
        "operator": filter_obj.operator,
        "value": filter_obj.value,
        "logic": filter_obj.logic,
    }


async def delete_filter(
    db: AsyncSession, table_id: Optional[str], filter_id: str
) -> bool:
    """Delete a filter by ID"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableFilter).where(TableFilter.id == filter_id, TableFilter.table_id == table_id)
    )
    filter_obj = result.scalars().first()
    if not filter_obj:
        return False

    await db.delete(filter_obj)
    await db.commit()
    return True


async def update_filters(
    db: AsyncSession, table_id: Optional[str], filters: List[Dict[str, Any]]
) -> bool:
    """Replace all filters with a new set"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    # Delete all existing filters
    await db.execute(delete(TableFilter).where(TableFilter.table_id == table_id))

    # Insert new filters
    for idx, filter_data in enumerate(filters):
        new_filter = TableFilter(
            id=filter_data["id"],
            table_id=table_id,
            field_id=filter_data["fieldId"],
            operator=filter_data["operator"],
            value=filter_data.get("value"),
            logic=filter_data.get("logic", "and"),
            order_index=idx,
        )
        db.add(new_filter)

    await db.commit()
    return True


async def clear_filters(
    db: AsyncSession, table_id: Optional[str]
) -> bool:
    """Delete all filters"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    await db.execute(delete(TableFilter).where(TableFilter.table_id == table_id))
    await db.commit()
    return True


async def get_sorts(
    db: AsyncSession, table_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Get all sorts from the database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableSort).where(TableSort.table_id == table_id).order_by(TableSort.order_index)
    )
    sorts = result.scalars().all()
    return [
        {
            "fieldId": s.field_id,
            "order": s.order,
        }
        for s in sorts
    ]


async def add_sort(
    db: AsyncSession, table_id: Optional[str], sort_data: Dict[str, Any]
) -> Dict[str, Any]:
    """Add a new sort to the database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    # Get max order
    result_max = await db.execute(
        select(func.max(TableSort.order_index)).where(TableSort.table_id == table_id)
    )
    max_order = result_max.scalar() or 0

    new_sort = TableSort(
        id=f"s{int(time.time() * 1000)}",
        table_id=table_id,
        field_id=sort_data["fieldId"],
        order=sort_data["order"],
        order_index=max_order + 1,
    )
    db.add(new_sort)
    await db.commit()
    return sort_data


async def delete_sort(
    db: AsyncSession, table_id: Optional[str], sort_id: str
) -> bool:
    """Delete a sort by ID"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableSort).where(TableSort.id == sort_id, TableSort.table_id == table_id)
    )
    sort_obj = result.scalars().first()
    if not sort_obj:
        return False

    await db.delete(sort_obj)
    await db.commit()
    return True


async def update_sorts(
    db: AsyncSession, table_id: Optional[str], sorts: List[Dict[str, Any]]
) -> bool:
    """Replace all sorts with a new set"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    # Delete all existing sorts
    await db.execute(delete(TableSort).where(TableSort.table_id == table_id))

    # Insert new sorts
    for idx, sort_data in enumerate(sorts):
        new_sort = TableSort(
            id=f"s{int(time.time() * 1000)}_{idx}",
            table_id=table_id,
            field_id=sort_data["fieldId"],
            order=sort_data["order"],
            order_index=idx,
        )
        db.add(new_sort)

    await db.commit()
    return True


async def clear_sorts(
    db: AsyncSession, table_id: Optional[str]
) -> bool:
    """Delete all sorts"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    await db.execute(delete(TableSort).where(TableSort.table_id == table_id))
    await db.commit()
    return True


async def update_group_config(
    db: AsyncSession, table_id: Optional[str], config: Dict[str, Any]
) -> Dict[str, Any]:
    """Update group config in database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    # Check if a group config exists
    result = await db.execute(
        select(TableGroup).where(TableGroup.table_id == table_id).limit(1)
    )
    existing_group = result.scalars().first()
    
    if existing_group:
        existing_group.field_id = config.get("fieldId")
        existing_group.order = config.get("order", "asc")
    else:
        # Create new
        new_group = TableGroup(
            id=f"grp_{table_id}",
            table_id=table_id,
            field_id=config.get("fieldId"),
            order=config.get("order", "asc")
        )
        db.add(new_group)
    
    await db.commit()
    
    return {
        "fieldId": config.get("fieldId"),
        "order": config.get("order", "asc")
    }


async def update_view_config(
    db: AsyncSession, table_id: Optional[str], view_id: str, config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Update view config in database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableView).where(TableView.id == view_id, TableView.table_id == table_id)
    )
    view = result.scalars().first()
    if not view:
        return None
    view.config = config
    await db.commit()
    return {
        "id": view.id,
        "name": view.name,
        "type": view.type,
        "config": view.config,
    }


async def _set_db_table_default_view_id(
    db: AsyncSession, table_id: str, view_id: str
) -> None:
    """Set default view ID for a table in database"""
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == table_id,
            WorkspaceItem.type == "table",
        )
    )
    table_item = result.scalars().first()
    if not table_item:
        return
    if table_item.default_view_id == view_id:
        return
    table_item.default_view_id = view_id
    await db.commit()


async def create_view(
    db: AsyncSession, table_id: Optional[str], view_data: Dict[str, Any]
) -> Dict[str, Any]:
    """Create a view in database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    new_view = TableView(
        id=view_data["id"],
        table_id=table_id,
        name=view_data["name"],
        type=view_data["type"],
        config=view_data.get("config"),
    )
    db.add(new_view)
    await db.commit()
    result_count = await db.execute(
        select(func.count()).where(TableView.table_id == table_id)
    )
    if (result_count.scalar() or 0) == 1:
        await _set_db_table_default_view_id(db, table_id, view_data["id"])
    return view_data


async def rename_view(
    db: AsyncSession, table_id: Optional[str], view_id: str, name: str
) -> Optional[Dict[str, Any]]:
    """Rename a view in database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableView).where(TableView.id == view_id, TableView.table_id == table_id)
    )
    view = result.scalars().first()
    if not view:
        return None
    view.name = name
    await db.commit()
    return {
        "id": view.id,
        "name": view.name,
        "type": view.type,
        "config": view.config,
    }


async def copy_view(
    db: AsyncSession, table_id: Optional[str], source_view_id: str, new_view_data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Copy a view in database"""
    from .helpers import normalize_table_id, _deep_clone
    
    table_id = normalize_table_id(table_id)
    result = await db.execute(
        select(TableView).where(
            TableView.id == source_view_id,
            TableView.table_id == table_id,
        )
    )
    source_view = result.scalars().first()
    if not source_view:
        return None
    db.add(
        TableView(
            id=new_view_data["id"],
            table_id=table_id,
            name=new_view_data["name"],
            type=new_view_data["type"],
            config=_deep_clone(new_view_data.get("config")),
        )
    )
    await db.commit()
    return new_view_data


async def delete_view(
    db: AsyncSession, table_id: Optional[str], view_id: str
) -> bool:
    """Delete a view in database"""
    from .helpers import normalize_table_id
    
    table_id = normalize_table_id(table_id)
    result_count = await db.execute(
        select(func.count()).where(TableView.table_id == table_id)
    )
    if (result_count.scalar() or 0) <= 1:
        return False
    result = await db.execute(
        select(TableView).where(TableView.id == view_id, TableView.table_id == table_id)
    )
    view = result.scalars().first()
    if not view:
        return False
    await db.delete(view)
    await db.commit()
    result_first = await db.execute(
        select(TableView).where(TableView.table_id == table_id).limit(1)
    )
    first_view = result_first.scalars().first()
    if first_view:
        await _set_db_table_default_view_id(db, table_id, first_view.id)
    return True
