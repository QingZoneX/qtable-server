import json
from typing import List, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _resolve_backend,
    _resolve_db_table_id,
    _resolve_table_id_for_backend,
    _table_exists,
    _require_item_permission,
    _require_record_permission,
    _require_user,
    _filter_store_by_row_permission,
    _row_permission_context,
    ensure_presence_cleanup_task,
    table_presence_registry,
)
from app.services.relation_engine import (
    get_display_field_id,
    get_target_table_id,
    is_relation_field,
    relation_allows_multiple,
)
from app.models.smart_table import TableField, TableRecord
from app.services.smart_table_store import (
    get_full_store,
    get_full_store_for_table,
    get_filters,
    get_sorts,
    init_table_db,
)
from app.services.record_query import (
    DEFAULT_QUERY_LIMIT,
    query_records_db,
    query_records_in_memory,
)
from app.services.row_permissions import (
    allowed_record_ids,
    get_row_permission_policy,
    row_permission_restricts_user,
    sanitize_relation_values_for_user,
)
from app.services.change_history import list_record_history, list_recycle_bin
from app.services.formula_engine import materialize_records
from app.services.workspace import permission_allows
from app.services.member_field import hydrate_member_fields_for_table


def _relation_title(value: object, record_id: str) -> str:
    if value is None or value == "":
        return record_id
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [str(item) for item in value if item is not None and item != ""]
        return ", ".join(parts) if parts else record_id
    if isinstance(value, dict):
        for key in ("label", "name", "title", "id"):
            candidate = value.get(key)
            if candidate is not None and candidate != "":
                return str(candidate)
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return record_id
    return str(value)


@strawberry.type
class TableQueries:
    """Mixin for table-related query resolvers"""

    @strawberry.field(name="recordHistory")
    async def record_history(
        self,
        info: Info,
        table_id: str,
        record_id: strawberry.ID,
        offset: int = 0,
        limit: int = 50,
    ) -> JSON:
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Record history requires database backend")
        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        permission = await _require_item_permission(
            info,
            resolved_table_id,
            "read",
        )

        active_result = await db.execute(
            select(TableRecord.id).where(
                TableRecord.table_id == resolved_table_id,
                TableRecord.id == str(record_id),
            )
        )
        if active_result.scalars().first() is not None:
            await _require_record_permission(
                info,
                resolved_table_id,
                str(record_id),
                "read",
            )
        elif not permission_allows(permission, "manage"):
            # Deleted-row history can contain the entire previous row snapshot;
            # expose it only through the recycle-bin management permission.
            raise GraphQLError("Record not found or no access")

        return await list_record_history(
            db,
            table_id=resolved_table_id,
            record_id=str(record_id),
            offset=offset,
            limit=limit,
        )

    @strawberry.field(name="recycleBin")
    async def recycle_bin(
        self,
        info: Info,
        table_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> JSON:
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Recycle bin requires database backend")
        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        await _require_item_permission(info, resolved_table_id, "manage")
        await _require_user(info)
        return await list_recycle_bin(
            db,
            table_id=resolved_table_id,
            offset=offset,
            limit=limit,
        )

    @strawberry.field(name="recordById")
    async def record_by_id(
        self,
        info: Info,
        table_id: str,
        record_id: strawberry.ID,
    ) -> JSON:
        """Read exactly one visible record for deep-link/detail workflows.

        This avoids progressively loading an entire large table just to open a
        global-search result. Missing and row-hidden records deliberately share
        the same access error.
        """
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Record lookup requires database backend")

        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        await _require_record_permission(
            info,
            resolved_table_id,
            str(record_id),
            "read",
        )
        user = await _require_user(info)

        field_result = await db.execute(
            select(TableField)
            .where(TableField.table_id == resolved_table_id)
            .order_by(TableField.order_index, TableField.id)
        )
        fields = [
            {
                "id": field.id,
                "name": field.name,
                "type": field.type,
                "options": field.options,
                "property": field.property,
            }
            for field in field_result.scalars().all()
        ]

        record_result = await db.execute(
            select(TableRecord).where(
                TableRecord.table_id == resolved_table_id,
                TableRecord.id == str(record_id),
            )
        )
        record = record_result.scalars().first()
        if record is None:
            # The permission helper above intentionally normalizes missing and
            # hidden rows, but keep this guard for concurrent deletion.
            raise GraphQLError("Record not found or no access")

        raw = {"id": record.id, **dict(record.data or {})}
        materialized = materialize_records(fields, [raw])
        sanitized = await sanitize_relation_values_for_user(
            db,
            fields,
            materialized,
            user_id=user.id,
        )
        if not sanitized:
            raise GraphQLError("Record not found or no access")
        return sanitized[0]

    @strawberry.field
    async def fields(self, info: Info, table_id: Optional[str] = None) -> List[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            if not await _table_exists(db, resolved_table_id):
                return []
            await _require_item_permission(info, resolved_table_id, "read")
        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, _resolve_db_table_id(table_id))
        )
        if backend == "db" and not store.get("fields"):
            # Repair legacy shell tables created by the old two-phase create
            # flow. init_table_db is idempotent and preserves existing views
            # and records while restoring the required core text field.
            resolved_table_id = _resolve_db_table_id(table_id)
            await init_table_db(db, resolved_table_id, "blank")
            store = await get_full_store(db, resolved_table_id)
        if backend == "db":
            return await hydrate_member_fields_for_table(
                db,
                _resolve_db_table_id(table_id),
                store["fields"],
            )
        return store["fields"]

    @strawberry.field
    async def records(self, info: Info, table_id: Optional[str] = None) -> List[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            if not await _table_exists(db, resolved_table_id):
                return []
            store = await get_full_store(db, resolved_table_id)
            filtered_store, _, _ = await _filter_store_by_row_permission(
                info,
                resolved_table_id,
                store,
                "read",
            )
            return filtered_store.get("records", [])

        store = await get_full_store_for_table(table_id)
        return store.get("records", [])

    @strawberry.field(name="queryRecords")
    async def query_records(
        self,
        info: Info,
        table_id: Optional[str] = None,
        filters: Optional[List[JSON]] = None,
        sorts: Optional[List[JSON]] = None,
        offset: int = 0,
        limit: int = DEFAULT_QUERY_LIMIT,
    ) -> JSON:
        """Filter/sort records on the server and return a paged result."""
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)

        normalized_filters = [dict(item) for item in (filters or []) if isinstance(item, dict)]
        normalized_sorts = [dict(item) for item in (sorts or []) if isinstance(item, dict)]
        if len(normalized_filters) != len(filters or []):
            raise GraphQLError("Every filter must be an object")
        if len(normalized_sorts) != len(sorts or []):
            raise GraphQLError("Every sort must be an object")

        try:
            if backend == "db":
                resolved_table_id = _resolve_db_table_id(table_id)
                if not await _table_exists(db, resolved_table_id):
                    return {
                        "records": [],
                        "totalCount": 0,
                        "offset": max(0, offset),
                        "limit": max(1, min(1000, limit)),
                        "executionMode": "sql",
                        "sqlFilterApplied": False,
                        "sqlSortApplied": False,
                    }
                user, table_permission, row_policy = await _row_permission_context(
                    info,
                    resolved_table_id,
                    "read",
                )
                visible_record_ids = None
                if row_permission_restricts_user(row_policy, table_permission):
                    visible_record_ids = await allowed_record_ids(
                        db,
                        resolved_table_id,
                        user_id=user.id,
                        table_permission=table_permission,
                        policy=row_policy,
                    )

                field_result = await db.execute(
                    select(TableField)
                    .where(TableField.table_id == resolved_table_id)
                    .order_by(TableField.order_index)
                )
                fields = [
                    {
                        "id": field.id,
                        "name": field.name,
                        "type": field.type,
                        "options": field.options,
                        "property": field.property,
                    }
                    for field in field_result.scalars().all()
                ]
                result = await query_records_db(
                    db,
                    resolved_table_id,
                    fields,
                    filters=normalized_filters,
                    sorts=normalized_sorts,
                    offset=offset,
                    limit=limit,
                    allowed_record_ids=visible_record_ids,
                )
                result["records"] = await sanitize_relation_values_for_user(
                    db,
                    fields,
                    list(result.get("records", [])),
                    user_id=user.id,
                )
                return result

            store = await get_full_store_for_table(table_id)
            result = query_records_in_memory(
                store.get("fields", []),
                store.get("records", []),
                filters=normalized_filters,
                sorts=normalized_sorts,
                offset=offset,
                limit=limit,
            )
            result.update(
                {
                    "executionMode": "memory",
                    "sqlFilterApplied": False,
                    "sqlSortApplied": False,
                }
            )
            return result
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field
    async def views(self, info: Info, table_id: Optional[str] = None) -> List[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            if not await _table_exists(db, resolved_table_id):
                return []
            await _require_item_permission(info, resolved_table_id, "read")
        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, _resolve_db_table_id(table_id))
        )
        return store["views"]

    @strawberry.field(name="hiddenFieldIds")
    async def hiddenFieldIds(self, info: Info, table_id: Optional[str] = None) -> List[str]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            if not await _table_exists(db, resolved_table_id):
                return []
            await _require_item_permission(info, resolved_table_id, "read")
        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, _resolve_db_table_id(table_id))
        )
        return store.get("hiddenFieldIds", [])

    @strawberry.field
    async def filters(self, info: Info, table_id: Optional[str] = None) -> List[JSON]:
        backend = _resolve_backend(table_id)
        if backend == "file" and table_id:
            store = await get_full_store_for_table(table_id)
            return store.get("filters", [])
        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        if not await _table_exists(db, resolved_table_id):
            return []
        await _require_item_permission(info, resolved_table_id, "read")
        return await get_filters(db, _resolve_db_table_id(table_id))

    @strawberry.field
    async def sorts(self, info: Info, table_id: Optional[str] = None) -> List[JSON]:
        backend = _resolve_backend(table_id)
        if backend == "file" and table_id:
            store = await get_full_store_for_table(table_id)
            return store.get("sorts", [])
        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        if not await _table_exists(db, resolved_table_id):
            return []
        await _require_item_permission(info, resolved_table_id, "read")
        return await get_sorts(db, _resolve_db_table_id(table_id))

    @strawberry.field(name="groupConfig")
    async def groupConfig(self, info: Info, table_id: Optional[str] = None) -> JSON:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            if not await _table_exists(db, resolved_table_id):
                return {"fieldId": None, "order": "asc"}
            await _require_item_permission(info, resolved_table_id, "read")
        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, _resolve_db_table_id(table_id))
        )
        return store.get("groupConfig")

    @strawberry.field(name="rowPermissionPolicy")
    async def row_permission_policy(
        self,
        info: Info,
        table_id: Optional[str] = None,
    ) -> JSON:
        backend = _resolve_backend(table_id)
        if backend != "db":
            return {"mode": "all", "memberFieldId": None, "enabled": False}

        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        if not await _table_exists(db, resolved_table_id):
            return {"mode": "all", "memberFieldId": None, "enabled": False}
        await _require_item_permission(info, resolved_table_id, "read")
        return await get_row_permission_policy(db, resolved_table_id)

    @strawberry.field(name="relationOptions")
    async def relationOptions(
        self,
        info: Info,
        table_id: str,
        field_id: str,
        search: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
        record_ids: Optional[List[str]] = None,
    ) -> JSON:
        """Return paged relation choices or resolve exact selected record IDs."""
        db: AsyncSession = info.context["db"]
        source_backend = _resolve_backend(table_id)
        if source_backend == "db":
            source_id = _resolve_db_table_id(table_id)
            if not await _table_exists(db, source_id):
                raise ValueError("Source table does not exist")
            await _require_item_permission(info, source_id, "read")
            source_store = await get_full_store(db, source_id)
        else:
            source_store = await get_full_store_for_table(table_id)

        field = next(
            (item for item in source_store.get("fields", []) if item.get("id") == field_id),
            None,
        )
        if not field or not is_relation_field(field):
            raise ValueError("Field is not a relation field")

        target_table_id = get_target_table_id(field)
        target_backend = _resolve_backend(target_table_id)
        if target_backend == "db":
            target_id = _resolve_db_table_id(target_table_id)
            if not await _table_exists(db, target_id):
                raise ValueError("Relation target table does not exist")
            target_store = await get_full_store(db, target_id)
            target_store, _, _ = await _filter_store_by_row_permission(
                info,
                target_id,
                target_store,
                "read",
            )
        else:
            target_store = await get_full_store_for_table(target_table_id)

        target_fields = target_store.get("fields", [])
        display_field_id = get_display_field_id(field)
        if not display_field_id and target_fields:
            display_field_id = target_fields[0].get("id")

        all_items = []
        for record in target_store.get("records", []):
            record_id = str(record.get("id") or "")
            if not record_id:
                continue
            title = _relation_title(
                record.get(display_field_id) if display_field_id else None,
                record_id,
            )
            all_items.append({"id": record_id, "title": title})

        # Existing selections must resolve independently of paging/search. This
        # path is intentionally bounded so clients can batch large tables in
        # chunks without turning one cell render into an unbounded query.
        if record_ids is not None:
            requested = []
            seen = set()
            for raw_id in record_ids[:200]:
                record_id = str(raw_id).strip()
                if record_id and record_id not in seen:
                    requested.append(record_id)
                    seen.add(record_id)
            by_id = {item["id"]: item for item in all_items}
            resolved = [by_id[record_id] for record_id in requested if record_id in by_id]
            return {
                "targetTableId": target_table_id,
                "displayFieldId": display_field_id,
                "multiple": relation_allows_multiple(field),
                "total": len(resolved),
                "hasMore": False,
                "items": resolved,
            }

        normalized_search = (search or "").strip().casefold()
        items = [
            item
            for item in all_items
            if not normalized_search
            or normalized_search in item["title"].casefold()
            or normalized_search in item["id"].casefold()
        ]

        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(200, int(limit)))
        page = items[safe_offset : safe_offset + safe_limit]
        return {
            "targetTableId": target_table_id,
            "displayFieldId": display_field_id,
            "multiple": relation_allows_multiple(field),
            "total": len(items),
            "hasMore": safe_offset + len(page) < len(items),
            "items": page,
        }

    @strawberry.field(name="tableViewers")
    async def tableViewers(self, info: Info, table_id: str) -> List[JSON]:
        backend = _resolve_backend(table_id)
        resolved_table_id = _resolve_table_id_for_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "read")
        await ensure_presence_cleanup_task()
        return await table_presence_registry.get_viewers(resolved_table_id)