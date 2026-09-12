from typing import Any, Dict, List, Optional
import base64
import uuid

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _resolve_backend,
    _resolve_db_table_id,
    _resolve_table_id_for_backend,
    _table_exists,
    _require_item_permission,
    _require_user,
    _filter_store_by_row_permission,
    _require_record_permission,
    _row_permission_context,
    publish_table_update,
    ensure_presence_cleanup_task,
    table_presence_registry,
    _presence_viewer_from_context,
    table_broker,
    yjs_broker,
    table_presence_broker,
)
from app.api.graphql.types import RecordPatchInput
from app.services.smart_table_store import (
    get_full_store,
    get_full_store_for_table,
    create_view,
    rename_view,
    copy_view,
    delete_view,
    create_record,
    create_records,
    create_records_with_data,
    update_record,
    update_record_fields,
    apply_record_patches,
    add_field,
    update_field,
    delete_field,
    delete_record,
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
    create_view_file,
    rename_view_file,
    copy_view_file,
    delete_view_file,
    create_record_file,
    create_records_file,
    create_records_with_data_file,
    update_record_file,
    apply_record_patches_file,
    add_field_file,
    update_field_file,
    delete_field_file,
    delete_record_file,
    reorder_fields_file,
    update_field_visibility_file,
    update_filters_file,
    clear_filters_file,
    update_sorts_file,
    clear_sorts_file,
    update_group_config_file,
    update_view_config_file,
    resolve_table_id,
)
from app.api.graphql.helpers import ALLOWED_VIEW_TYPES, DEFAULT_VIEW_NAMES, _next_view_name, _build_default_view_config
from app.services.csv_import import (
    CsvImportError,
    build_csv_preview,
    prepare_csv_import,
)
from app.services.xlsx_export import XLSX_MIME_TYPE, build_xlsx_export
from app.services.calendar_range import (
    CalendarRangeError,
    validate_calendar_range_update,
)
from app.services.row_permissions import (
    ensure_permission_field_change_safe,
    row_permission_restricts_user,
    set_row_permission_policy,
)
from app.services.change_history import (
    ChangeConflictError,
    ChangeNotUndoableError,
    get_change_items,
    get_change_set,
    purge_recycled_record,
    restore_recycled_record,
    undo_change_set as undo_change_set_service,
)
from app.services.workspace import permission_allows


@strawberry.type
class TableMutations:
    """Mixin for table-related mutation resolvers"""
    
    @strawberry.mutation
    async def create_view(
        self,
        info: Info,
        view_type: str,
        table_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        resolved_table_id = table_id if backend == "file" and table_id else _resolve_db_table_id(table_id)
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "edit")
        normalized_type = (view_type or "").strip().lower()
        if normalized_type == "kanban":
            normalized_type = "board"
        if normalized_type not in ALLOWED_VIEW_TYPES:
            raise GraphQLError("Invalid view type")
        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, _resolve_db_table_id(table_id))
        )
        existing_views = store.get("views", [])
        existing_names = [view.get("name") for view in existing_views if isinstance(view, dict)]
        base_name = (name or "").strip() or DEFAULT_VIEW_NAMES[normalized_type]
        next_name = _next_view_name(base_name, existing_names)
        view_data = {
            "id": f"v{uuid.uuid4().hex[:10]}",
            "name": next_name,
            "type": normalized_type,
            "config": _build_default_view_config(normalized_type),
        }
        created = await (
            create_view_file(table_id, view_data)
            if backend == "file" and table_id
            else create_view(db, _resolve_db_table_id(table_id), view_data)
        )
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return created

    @strawberry.mutation
    async def rename_view(
        self,
        info: Info,
        view_id: str,
        name: str,
        table_id: Optional[str] = None,
    ) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        resolved_table_id = table_id if backend == "file" and table_id else _resolve_db_table_id(table_id)
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "edit")
        next_name = (name or "").strip()
        if not next_name:
            raise GraphQLError("View name is required")
        updated = await (
            rename_view_file(table_id, view_id, next_name)
            if backend == "file" and table_id
            else rename_view(db, _resolve_db_table_id(table_id), view_id, next_name)
        )
        if updated is None:
            return None
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation
    async def copy_view(
        self,
        info: Info,
        view_id: str,
        table_id: Optional[str] = None,
        new_name: Optional[str] = None,
    ) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        resolved_table_id = table_id if backend == "file" and table_id else _resolve_db_table_id(table_id)
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "edit")
        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, _resolve_db_table_id(table_id))
        )
        existing_views = store.get("views", [])
        source = next(
            (
                view
                for view in existing_views
                if isinstance(view, dict) and view.get("id") == view_id
            ),
            None,
        )
        if not source:
            return None
        base_name = (new_name or "").strip() or f'{source.get("name", "View")} Copy'
        existing_names = [view.get("name") for view in existing_views if isinstance(view, dict)]
        copied = {
            "id": f"v{uuid.uuid4().hex[:10]}",
            "name": _next_view_name(base_name, existing_names),
            "type": source.get("type", "grid"),
            "config": source.get("config") or _build_default_view_config(source.get("type", "grid")),
        }
        created = await (
            copy_view_file(table_id, view_id, copied)
            if backend == "file" and table_id
            else copy_view(db, _resolve_db_table_id(table_id), view_id, copied)
        )
        if created is None:
            return None
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return created

    @strawberry.mutation
    async def delete_view(
        self,
        info: Info,
        view_id: str,
        table_id: Optional[str] = None,
    ) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        resolved_table_id = table_id if backend == "file" and table_id else _resolve_db_table_id(table_id)
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "edit")
        deleted = await (
            delete_view_file(table_id, view_id)
            if backend == "file" and table_id
            else delete_view(db, _resolve_db_table_id(table_id), view_id)
        )
        if not deleted:
            return False
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return True

    @strawberry.mutation
    async def update_record(self, info: Info, record_id: strawberry.ID, field_id: str, value: Optional[JSON], table_id: Optional[str] = None) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        current_user_id: Optional[int] = None
        if backend == "db":
            await _require_record_permission(
                info,
                _resolve_db_table_id(table_id),
                str(record_id),
                "update",
            )
            current_user_id = (await _require_user(info)).id
        updated = await (
            update_record_file(table_id, str(record_id), field_id, value)
            if backend == "file" and table_id
            else update_record(
                db,
                _resolve_db_table_id(table_id),
                str(record_id),
                field_id,
                value,
                user_id=current_user_id,
                source="graphql",
            )
        )
        if updated is None:
            return None
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation
    async def update_calendar_range(
        self,
        info: Info,
        record_id: strawberry.ID,
        start_field_id: str,
        start_value: Optional[JSON] = None,
        end_field_id: Optional[str] = None,
        end_value: Optional[JSON] = None,
        table_id: Optional[str] = None,
    ) -> Optional[JSON]:
        """Atomically update a calendar task's start/end values."""
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        resolved_table_id = (
            table_id
            if backend == "file" and table_id
            else _resolve_db_table_id(table_id)
        )
        current_user_id: Optional[int] = None
        if backend == "db":
            await _require_record_permission(
                info,
                resolved_table_id,
                str(record_id),
                "update",
            )
            current_user_id = (await _require_user(info)).id

        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, resolved_table_id)
        )
        try:
            validate_calendar_range_update(
                store.get("fields", []),
                start_field_id=start_field_id,
                end_field_id=end_field_id,
                start_value=start_value,
                end_value=end_value,
            )
        except CalendarRangeError as exc:
            raise GraphQLError(str(exc)) from exc

        patches: List[Dict[str, Any]] = [
            {
                "recordId": str(record_id),
                "fieldId": start_field_id,
                "value": start_value,
            }
        ]
        if end_field_id:
            patches.append(
                {
                    "recordId": str(record_id),
                    "fieldId": end_field_id,
                    "value": end_value,
                }
            )

        updated_count = await (
            apply_record_patches_file(resolved_table_id, patches)
            if backend == "file" and table_id
            else apply_record_patches(
                db,
                resolved_table_id,
                patches,
                user_id=current_user_id,
                source="calendar",
            )
        )
        if not updated_count:
            return None

        next_store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, resolved_table_id)
        )
        updated = next(
            (
                record
                for record in next_store.get("records", [])
                if str(record.get("id")) == str(record_id)
            ),
            None,
        )
        await publish_table_update(db, resolved_table_id)
        return updated

    @strawberry.mutation
    async def export_xlsx(
        self,
        info: Info,
        table_id: Optional[str] = None,
        field_ids: Optional[List[str]] = None,
        record_ids: Optional[List[str]] = None,
    ) -> JSON:
        """Generate a real Office Open XML workbook for the requested table rows."""
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        resolved_table_id = (
            table_id
            if backend == "file" and table_id
            else _resolve_db_table_id(table_id)
        )
        if backend == "db" and not await _table_exists(db, resolved_table_id):
            raise GraphQLError("Table does not exist")

        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, resolved_table_id)
        )
        if backend == "db":
            store, _, _ = await _filter_store_by_row_permission(
                info,
                resolved_table_id,
                store,
                "read",
            )
        try:
            workbook = build_xlsx_export(
                store.get("fields", []),
                store.get("records", []),
                field_ids=field_ids,
                record_ids=record_ids,
            )
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc

        return {
            "contentBase64": base64.b64encode(workbook).decode("ascii"),
            "mimeType": XLSX_MIME_TYPE,
            "rowCount": len(record_ids) if record_ids is not None else len(store.get("records", [])),
            "fieldCount": len(field_ids) if field_ids is not None else len(store.get("fields", [])),
        }

    @strawberry.mutation
    async def insert_row(self, info: Info, table_id: Optional[str] = None, data: Optional[JSON] = None) -> JSON:
        """Insert one row; DB mode records the create as one ChangeSet."""
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        creator_user_id: Optional[int] = None
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            await _require_item_permission(info, resolved_table_id, "update")
            creator_user_id = (await _require_user(info)).id

        if backend == "db" and isinstance(data, dict) and data:
            # Avoid create-then-update becoming two history entries. The batch
            # create path writes the row and its initial values atomically.
            created = await create_records_with_data(
                db,
                _resolve_db_table_id(table_id),
                [dict(data)],
                created_by_user_id=creator_user_id,
                source="graphql",
            )
            record = created[0]
        else:
            record = await (
                create_record_file(table_id)
                if backend == "file" and table_id
                else create_record(
                    db,
                    _resolve_db_table_id(table_id),
                    created_by_user_id=creator_user_id,
                    source="graphql",
                )
            )

            if data and isinstance(data, dict):
                resolved_table_id = table_id if backend == "file" else _resolve_db_table_id(table_id)
                record_id = record.get("id")
                if backend == "file":
                    record = await update_record_file(resolved_table_id, record_id, data)
                else:
                    record = await update_record_fields(
                        db,
                        resolved_table_id,
                        record_id,
                        data,
                        user_id=creator_user_id,
                        source="graphql",
                    )

        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return record

    @strawberry.mutation
    async def insert_rows(self, info: Info, count: int = 1, table_id: Optional[str] = None) -> List[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        creator_user_id: Optional[int] = None
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            await _require_item_permission(info, resolved_table_id, "update")
            creator_user_id = (await _require_user(info)).id
        records = await (
            create_records_file(table_id, count)
            if backend == "file" and table_id
            else create_records(
                db,
                _resolve_db_table_id(table_id),
                count,
                created_by_user_id=creator_user_id,
                source="graphql",
            )
        )
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return records

    @strawberry.mutation
    async def insert_rows_with_data(self, info: Info, table_id: Optional[str] = None, records_data: Optional[List[JSON]] = None) -> List[JSON]:
        """批量插入记录并附带初始数据。records_data 为字典列表，每个字典的键为字段ID，值为字段数据。"""
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        creator_user_id: Optional[int] = None
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            await _require_item_permission(info, resolved_table_id, "update")
            creator_user_id = (await _require_user(info)).id

        if not records_data or not isinstance(records_data, list):
            return []

        # 过滤掉内部元数据键
        clean_records: List[Dict[str, Any]] = []
        for rd in records_data:
            if isinstance(rd, dict):
                clean = {k: v for k, v in rd.items() if not k.startswith("_")}
                clean_records.append(clean)

        if not clean_records:
            return []

        records = await (
            create_records_with_data_file(table_id, clean_records)
            if backend == "file" and table_id
            else create_records_with_data(
                db,
                _resolve_db_table_id(table_id),
                clean_records,
                created_by_user_id=creator_user_id,
                source="graphql",
            )
        )
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return records

    @strawberry.mutation
    async def preview_csv_import(
        self,
        info: Info,
        csv_text: str,
        table_id: Optional[str] = None,
        mapping: Optional[List[JSON]] = None,
        has_header: bool = True,
        delimiter: Optional[str] = "auto",
    ) -> JSON:
        """Parse CSV and optionally validate a source-column -> field mapping."""
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        resolved_table_id = (
            table_id
            if backend == "file" and table_id
            else _resolve_db_table_id(table_id)
        )
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "update")

        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, resolved_table_id)
        )
        try:
            return build_csv_preview(
                csv_text,
                store.get("fields", []),
                mapping=mapping,
                has_header=has_header,
                delimiter=delimiter,
            )
        except CsvImportError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation
    async def import_csv(
        self,
        info: Info,
        csv_text: str,
        mapping: List[JSON],
        table_id: Optional[str] = None,
        has_header: bool = True,
        delimiter: Optional[str] = "auto",
        skip_invalid_rows: bool = False,
    ) -> JSON:
        """Validate the full CSV before writing any rows.

        By default any invalid row blocks the whole import. When
        skip_invalid_rows=True, only rows that passed all mapped-field
        conversions are inserted.
        """
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        resolved_table_id = (
            table_id
            if backend == "file" and table_id
            else _resolve_db_table_id(table_id)
        )
        creator_user_id: Optional[int] = None
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "update")
            creator_user_id = (await _require_user(info)).id

        store = await (
            get_full_store_for_table(table_id)
            if backend == "file" and table_id
            else get_full_store(db, resolved_table_id)
        )
        try:
            prepared = prepare_csv_import(
                csv_text,
                store.get("fields", []),
                mapping,
                has_header=has_header,
                delimiter=delimiter,
            )
        except CsvImportError as exc:
            raise GraphQLError(str(exc)) from exc

        error_count = int(prepared.get("errorCount") or 0)
        invalid_row_count = int(prepared.get("invalidRowCount") or 0)
        total_rows = int(prepared.get("rowCount") or 0)
        valid_records = list(prepared.get("records") or [])

        if error_count and not skip_invalid_rows:
            return {
                "success": False,
                "importedCount": 0,
                "skippedCount": invalid_row_count,
                "totalRows": total_rows,
                "validRowCount": int(prepared.get("validRowCount") or 0),
                "invalidRowCount": invalid_row_count,
                "errorCount": error_count,
                "errors": prepared.get("errors", []),
                "warnings": prepared.get("warnings", []),
                "detectedDelimiter": prepared.get("detectedDelimiter"),
            }

        if not valid_records:
            return {
                "success": False,
                "importedCount": 0,
                "skippedCount": invalid_row_count,
                "totalRows": total_rows,
                "validRowCount": 0,
                "invalidRowCount": invalid_row_count,
                "errorCount": error_count,
                "errors": prepared.get("errors", []),
                "warnings": prepared.get("warnings", []),
                "detectedDelimiter": prepared.get("detectedDelimiter"),
            }

        created = await (
            create_records_with_data_file(table_id, valid_records)
            if backend == "file" and table_id
            else create_records_with_data(
                db,
                resolved_table_id,
                valid_records,
                created_by_user_id=creator_user_id,
                source="csv_import",
            )
        )
        await publish_table_update(
            db,
            table_id if backend == "file" else resolved_table_id,
        )

        return {
            "success": True,
            "importedCount": len(created),
            "skippedCount": invalid_row_count if skip_invalid_rows else 0,
            "totalRows": total_rows,
            "validRowCount": len(created),
            "invalidRowCount": invalid_row_count,
            "errorCount": error_count,
            "errors": prepared.get("errors", []),
            "warnings": prepared.get("warnings", []),
            "detectedDelimiter": prepared.get("detectedDelimiter"),
        }

    @strawberry.mutation(name="updateRowPermissionPolicy")
    async def update_row_permission_policy(
        self,
        info: Info,
        table_id: str,
        mode: str,
        member_field_id: Optional[str] = None,
    ) -> JSON:
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Row-level permissions require database backend")

        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        if not await _table_exists(db, resolved_table_id):
            raise GraphQLError("Table does not exist")
        await _require_item_permission(info, resolved_table_id, "manage")
        try:
            policy = await set_row_permission_policy(
                db,
                resolved_table_id,
                mode=mode,
                member_field_id=member_field_id,
            )
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc

        await publish_table_update(db, resolved_table_id)
        return policy

    @strawberry.mutation
    async def add_field(self, info: Info, field: JSON, table_id: Optional[str] = None, index: Optional[int] = None) -> JSON:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        created = await (
            add_field_file(table_id, field, index)
            if backend == "file" and table_id
            else add_field(db, _resolve_db_table_id(table_id), field, index)
        )
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return created

    @strawberry.mutation
    async def update_field(self, info: Info, field_id: str, updates: JSON, table_id: Optional[str] = None) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            await _require_item_permission(info, resolved_table_id, "edit")
            next_type = (
                str(updates.get("type"))
                if isinstance(updates, dict) and updates.get("type") is not None
                else None
            )
            try:
                await ensure_permission_field_change_safe(
                    db,
                    resolved_table_id,
                    field_id,
                    next_type=next_type,
                )
            except ValueError as exc:
                raise GraphQLError(str(exc)) from exc
        updated = await (
            update_field_file(table_id, field_id, updates)
            if backend == "file" and table_id
            else update_field(db, _resolve_db_table_id(table_id), field_id, updates)
        )
        if updated is None:
            return None
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation
    async def delete_field(self, info: Info, field_id: str, table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            resolved_table_id = _resolve_db_table_id(table_id)
            await _require_item_permission(info, resolved_table_id, "edit")
            try:
                await ensure_permission_field_change_safe(
                    db,
                    resolved_table_id,
                    field_id,
                    deleting=True,
                )
            except ValueError as exc:
                raise GraphQLError(str(exc)) from exc
        deleted = await (
            delete_field_file(table_id, field_id)
            if backend == "file" and table_id
            else delete_field(db, _resolve_db_table_id(table_id), field_id)
        )
        if deleted:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return deleted

    @strawberry.mutation
    async def delete_record(self, info: Info, record_id: strawberry.ID, table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        current_user_id: Optional[int] = None
        if backend == "db":
            await _require_record_permission(
                info,
                _resolve_db_table_id(table_id),
                str(record_id),
                "update",
            )
            current_user_id = (await _require_user(info)).id
        deleted = await (
            delete_record_file(table_id, str(record_id))
            if backend == "file" and table_id
            else delete_record(
                db,
                _resolve_db_table_id(table_id),
                str(record_id),
                user_id=current_user_id,
                source="graphql",
            )
        )
        if deleted:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return deleted

    @strawberry.mutation
    async def reorder_fields(self, info: Info, field_order: List[str], table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        updated = await (
            reorder_fields_file(table_id, field_order)
            if backend == "file" and table_id
            else reorder_fields(db, _resolve_db_table_id(table_id), field_order)
        )
        if updated:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation
    async def update_field_visibility(self, info: Info, hidden_field_ids: List[str], table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        updated = await (
            update_field_visibility_file(table_id, hidden_field_ids)
            if backend == "file" and table_id
            else update_field_visibility(db, _resolve_db_table_id(table_id), hidden_field_ids)
        )
        if updated:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation
    async def add_filter(self, info: Info, filter: JSON, table_id: Optional[str] = None) -> JSON:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        if backend == "file" and table_id:
            store = await get_full_store_for_table(table_id)
            next_filters = list(store.get("filters", [])) + [filter]
            await update_filters_file(table_id, next_filters)
            created = filter
        else:
            created = await add_filter(db, _resolve_db_table_id(table_id), filter)
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return created

    @strawberry.mutation
    async def update_filter(self, info: Info, filter_id: str, updates: JSON, table_id: Optional[str] = None) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        if backend == "file" and table_id:
            store = await get_full_store_for_table(table_id)
            filters = store.get("filters", [])
            for f in filters:
                if f.get("id") == filter_id:
                    f.update(updates or {})
                    await update_filters_file(table_id, filters)
                    updated = f
                    break
            else:
                updated = None
        else:
            updated = await update_filter(db, _resolve_db_table_id(table_id), filter_id, updates)
        if updated is None:
            return None
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation
    async def delete_filter(self, info: Info, filter_id: str, table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        if backend == "file" and table_id:
            store = await get_full_store_for_table(table_id)
            filters = store.get("filters", [])
            before = len(filters)
            filters = [f for f in filters if f.get("id") != filter_id]
            await update_filters_file(table_id, filters)
            deleted = len(filters) < before
        else:
            deleted = await delete_filter(db, _resolve_db_table_id(table_id), filter_id)
        if deleted:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return deleted

    @strawberry.mutation
    async def update_filters(self, info: Info, filters: List[JSON], table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        updated = await (
            update_filters_file(table_id, filters)
            if backend == "file" and table_id
            else update_filters(db, _resolve_db_table_id(table_id), filters)
        )
        if updated:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation
    async def clear_filters(self, info: Info, table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        cleared = await (
            clear_filters_file(table_id)
            if backend == "file" and table_id
            else clear_filters(db, _resolve_db_table_id(table_id))
        )
        if cleared:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return cleared

    @strawberry.mutation
    async def add_sort(self, info: Info, sort: JSON, table_id: Optional[str] = None) -> JSON:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        if backend == "file" and table_id:
            store = await get_full_store_for_table(table_id)
            next_sorts = list(store.get("sorts", [])) + [sort]
            await update_sorts_file(table_id, next_sorts)
            created = sort
        else:
            created = await add_sort(db, _resolve_db_table_id(table_id), sort)
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return created

    @strawberry.mutation
    async def delete_sort(self, info: Info, sort_id: str, table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        if backend == "file" and table_id:
            store = await get_full_store_for_table(table_id)
            sorts = store.get("sorts", [])
            before = len(sorts)
            sorts = [s for s in sorts if s.get("id") != sort_id]
            await update_sorts_file(table_id, sorts)
            deleted = len(sorts) < before
        else:
            deleted = await delete_sort(db, _resolve_db_table_id(table_id), sort_id)
        if deleted:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return deleted

    @strawberry.mutation
    async def update_sorts(self, info: Info, sorts: List[JSON], table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        updated = await (
            update_sorts_file(table_id, sorts)
            if backend == "file" and table_id
            else update_sorts(db, _resolve_db_table_id(table_id), sorts)
        )
        if updated:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation
    async def clear_sorts(self, info: Info, table_id: Optional[str] = None) -> bool:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        cleared = await (
            clear_sorts_file(table_id)
            if backend == "file" and table_id
            else clear_sorts(db, _resolve_db_table_id(table_id))
        )
        if cleared:
            await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return cleared

    @strawberry.mutation
    async def update_group_config(self, info: Info, config: JSON, table_id: Optional[str] = None) -> JSON:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        updated = await (
            update_group_config_file(table_id, config)
            if backend == "file" and table_id
            else update_group_config(db, _resolve_db_table_id(table_id), config)
        )
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation
    async def update_view_config(self, info: Info, view_id: str, config: JSON, table_id: Optional[str] = None) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        backend = _resolve_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, _resolve_db_table_id(table_id), "edit")
        updated = await (
            update_view_config_file(table_id, view_id, config)
            if backend == "file" and table_id
            else update_view_config(db, _resolve_db_table_id(table_id), view_id, config)
        )
        if updated is None:
            return None
        await publish_table_update(db, table_id if backend == "file" else _resolve_db_table_id(table_id))
        return updated

    @strawberry.mutation(name="undoChangeSet")
    async def undo_change_set(
        self,
        info: Info,
        change_set_id: str,
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        change_set = await get_change_set(db, change_set_id)
        if not change_set or not change_set.table_id:
            raise GraphQLError("ChangeSet not found")

        user = await _require_user(info)
        permission = await _require_item_permission(
            info,
            change_set.table_id,
            "update",
        )
        is_manager = permission_allows(permission, "manage")
        if change_set.actor_id is not None and change_set.actor_id != user.id and not is_manager:
            raise GraphQLError("Only the original actor or a table manager can undo this change")

        items = await get_change_items(db, change_set.id)
        if not items:
            raise GraphQLError("ChangeSet has no reversible items")

        # Normal update/create changes must still be visible to the caller.
        # Delete ChangeSets may contain system-generated reverse-relation
        # cleanup in other tables; the original actor can invert that system
        # side effect without gaining read access to those rows.
        if change_set.operation != "delete" and not is_manager:
            checked = set()
            for item in items:
                if item.after_data is None:
                    continue
                key = (item.table_id, item.entity_id)
                if key in checked:
                    continue
                await _require_record_permission(
                    info,
                    item.table_id,
                    item.entity_id,
                    "update",
                )
                checked.add(key)

        try:
            result = await undo_change_set_service(
                db,
                change_set_id=change_set.id,
                actor_id=user.id,
                source="graphql",
            )
        except ChangeConflictError as exc:
            raise GraphQLError(f"UNDO_CONFLICT: {exc}") from exc
        except ChangeNotUndoableError as exc:
            raise GraphQLError(str(exc)) from exc

        for changed_table_id in sorted({item.table_id for item in items}):
            await publish_table_update(db, changed_table_id)
        return result

    @strawberry.mutation(name="restoreRecord")
    async def restore_record(
        self,
        info: Info,
        table_id: str,
        record_id: strawberry.ID,
    ) -> Optional[JSON]:
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Recycle bin requires database backend")
        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        await _require_item_permission(info, resolved_table_id, "manage")
        user = await _require_user(info)
        try:
            result = await restore_recycled_record(
                db,
                table_id=resolved_table_id,
                record_id=str(record_id),
                actor_id=user.id,
            )
        except ChangeConflictError as exc:
            raise GraphQLError(f"RESTORE_CONFLICT: {exc}") from exc
        except ChangeNotUndoableError as exc:
            raise GraphQLError(str(exc)) from exc
        if result is not None:
            affected_table_ids = result.get("affectedTableIds") or [resolved_table_id]
            for changed_table_id in affected_table_ids:
                await publish_table_update(db, str(changed_table_id))
        return result

    @strawberry.mutation(name="purgeRecord")
    async def purge_record(
        self,
        info: Info,
        table_id: str,
        record_id: strawberry.ID,
        confirm_record_id: str,
    ) -> bool:
        if str(record_id) != str(confirm_record_id):
            raise GraphQLError("Permanent delete confirmation does not match record ID")
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Recycle bin requires database backend")
        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        await _require_item_permission(info, resolved_table_id, "manage")
        user = await _require_user(info)
        purged = await purge_recycled_record(
            db,
            table_id=resolved_table_id,
            record_id=str(record_id),
            actor_id=user.id,
        )
        if purged:
            await publish_table_update(db, resolved_table_id)
        return purged

    @strawberry.mutation(name="publishYjsUpdate")
    async def publishYjsUpdate(
        self,
        info: Info,
        doc_id: str,
        update: str,
        client_id: Optional[str] = None,
        record_patches: Optional[List[RecordPatchInput]] = None,
    ) -> bool:
        db: AsyncSession = info.context["db"]
        table_id = resolve_table_id(doc_id) or doc_id
        backend = _resolve_backend(table_id)
        resolved_table_id = (
            _resolve_db_table_id(table_id) if backend == "db" else table_id
        )

        restricted_row_scope = False
        current_user_id: Optional[int] = None
        if backend == "db":
            current_user, table_permission, row_policy = await _row_permission_context(
                info,
                resolved_table_id,
                "update",
            )
            current_user_id = current_user.id
            restricted_row_scope = row_permission_restricts_user(
                row_policy,
                table_permission,
            )

        patches: List[Dict[str, Any]] = []
        if record_patches:
            patches = [
                {
                    "recordId": str(patch.record_id),
                    "fieldId": patch.field_id,
                    "value": patch.value,
                }
                for patch in record_patches
            ]

            if backend == "db":
                # Authorize every row before mutating any of them. This keeps a
                # mixed authorized/unauthorized batch atomic from the user's
                # point of view.
                checked_ids = set()
                for patch in patches:
                    record_id = str(patch["recordId"])
                    if record_id in checked_ids:
                        continue
                    await _require_record_permission(
                        info,
                        resolved_table_id,
                        record_id,
                        "update",
                    )
                    checked_ids.add(record_id)
                await apply_record_patches(
                    db,
                    resolved_table_id,
                    patches,
                    user_id=current_user_id,
                    source="realtime",
                )
            elif backend == "file" and table_id:
                await apply_record_patches_file(table_id, patches)

        # A Yjs binary update may encode a full CRDT snapshot and cannot be
        # safely row-filtered. Restricted users therefore synchronize through
        # authorized recordPatches + row-filtered tableUpdates only.
        yjs_payload: Dict[str, Any] = {
            "docId": doc_id,
            "update": "" if restricted_row_scope else update,
            "clientId": client_id,
        }
        if patches:
            yjs_payload["recordPatches"] = patches
        await yjs_broker.publish(yjs_payload)

        if patches:
            await publish_table_update(db, resolved_table_id)
        return True

    @strawberry.mutation(name="upsertTablePresence")
    async def upsertTablePresence(
        self,
        info: Info,
        table_id: str,
        session_id: str,
    ) -> List[JSON]:
        backend = _resolve_backend(table_id)
        resolved_table_id = _resolve_table_id_for_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "read")
        await ensure_presence_cleanup_task()
        viewer = _presence_viewer_from_context(info, session_id)
        event, changed = await table_presence_registry.touch(
            resolved_table_id,
            session_id,
            viewer,
        )
        if changed:
            await table_presence_broker.publish(event)
        return event.get("viewers", [])

    @strawberry.mutation(name="clearTablePresence")
    async def clearTablePresence(
        self,
        info: Info,
        table_id: str,
        session_id: str,
    ) -> bool:
        backend = _resolve_backend(table_id)
        resolved_table_id = _resolve_table_id_for_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "read")
        await ensure_presence_cleanup_task()
        event = await table_presence_registry.remove(resolved_table_id, session_id)
        if not event:
            return False
        await table_presence_broker.publish(event)
        return True
