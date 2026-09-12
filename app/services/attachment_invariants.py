from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app.models.attachment import AttachmentObject
from app.models.smart_table import TableField, TableRecord
from app.services.attachment_storage import ACTIVE_STATUS


class AttachmentReferenceIntegrityError(ValueError):
    """Raised when record JSON attempts to persist an invalid attachment reference."""


def _canonical_metadata(registry: Any) -> dict[str, Any]:
    if isinstance(registry, AttachmentObject):
        return {
            "attachmentId": registry.id,
            "objectKey": registry.object_key,
            "name": registry.filename,
            "size": int(registry.size or 0),
            "contentType": registry.content_type or "application/octet-stream",
        }
    return {
        "attachmentId": registry["id"],
        "objectKey": registry["object_key"],
        "name": registry["filename"],
        "size": int(registry["size"] or 0),
        "contentType": registry["content_type"] or "application/octet-stream",
    }


def _registry_scope(registry: Any) -> tuple[str, str, str, str, str]:
    if isinstance(registry, AttachmentObject):
        return (
            str(registry.table_id),
            str(registry.record_id),
            str(registry.field_id),
            str(registry.object_key),
            str(registry.status),
        )
    return (
        str(registry["table_id"]),
        str(registry["record_id"]),
        str(registry["field_id"]),
        str(registry["object_key"]),
        str(registry["status"]),
    )


def _changed_attachment_fields(record: TableRecord, attachment_fields: set[str]) -> set[str]:
    if inspect(record).pending:
        return {field_id for field_id in attachment_fields if (record.data or {}).get(field_id)}

    history = inspect(record).attrs.data.history
    if not history.has_changes():
        return set()
    new_data = dict(record.data or {})
    old_data = dict(history.deleted[0] or {}) if history.deleted else {}
    if not history.deleted:
        # Conservative fallback for an unusual SQLAlchemy history state: validate
        # attachment values currently present rather than allow an unverified write.
        return {field_id for field_id in attachment_fields if field_id in new_data}
    return {
        field_id
        for field_id in attachment_fields
        if old_data.get(field_id) != new_data.get(field_id)
    }


def _active_registry_ids_for_cell(
    *,
    connection,
    in_session_registry: dict[str, AttachmentObject],
    record: TableRecord,
    field_id: str,
) -> set[str]:
    """Return the transaction-current ACTIVE registry set bound to one cell.

    Database rows are overlaid with in-session registry objects so an attachment
    being marked delete/purge-pending in the same flush is treated as inactive,
    while newly-created ACTIVE objects are included. This makes record JSON and the
    registry an exact-set invariant instead of merely validating references that the
    caller happened to include.
    """
    persisted = connection.execute(
        select(AttachmentObject.__table__).where(
            AttachmentObject.table_id == str(record.table_id),
            AttachmentObject.record_id == str(record.id),
            AttachmentObject.field_id == str(field_id),
        )
    ).mappings().all()
    registry_by_id: dict[str, Any] = {str(item["id"]): item for item in persisted}

    for attachment_id, registry in in_session_registry.items():
        table_id, record_id, registry_field_id, _, _ = _registry_scope(registry)
        if (
            table_id == str(record.table_id)
            and record_id == str(record.id)
            and registry_field_id == str(field_id)
        ):
            registry_by_id[str(attachment_id)] = registry

    return {
        attachment_id
        for attachment_id, registry in registry_by_id.items()
        if _registry_scope(registry)[4] == ACTIVE_STATUS
    }


def _validate_attachment_value(
    *,
    connection,
    in_session_registry: dict[str, AttachmentObject],
    record: TableRecord,
    field_id: str,
    value: Any,
) -> list[dict[str, Any]]:
    if value is None:
        items: list[Any] = []
    elif not isinstance(value, list):
        raise AttachmentReferenceIntegrityError("Attachment field must be a list")
    else:
        items = value

    canonical: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise AttachmentReferenceIntegrityError("Attachment item must be an object")
        if "url" in item:
            raise AttachmentReferenceIntegrityError(
                "Expiring or direct attachment URLs cannot be persisted"
            )
        attachment_id = item.get("attachmentId")
        object_key = item.get("objectKey")
        if not isinstance(attachment_id, str) or not attachment_id:
            raise AttachmentReferenceIntegrityError("Attachment ID is required")
        if not isinstance(object_key, str) or not object_key:
            raise AttachmentReferenceIntegrityError("Attachment object key is required")
        if attachment_id in seen:
            raise AttachmentReferenceIntegrityError("Duplicate attachment reference")
        seen.add(attachment_id)

        registry: Any = in_session_registry.get(attachment_id)
        if registry is None:
            registry = (
                connection.execute(
                    select(AttachmentObject.__table__).where(
                        AttachmentObject.id == attachment_id
                    )
                )
                .mappings()
                .first()
            )
        if registry is None:
            raise AttachmentReferenceIntegrityError("Attachment registry entry is missing")

        table_id, record_id, registry_field_id, registry_key, registry_status = _registry_scope(
            registry
        )
        if registry_status != ACTIVE_STATUS:
            raise AttachmentReferenceIntegrityError("Attachment registry entry is inactive")
        if (
            table_id != str(record.table_id)
            or record_id != str(record.id)
            or registry_field_id != str(field_id)
            or registry_key != object_key
        ):
            raise AttachmentReferenceIntegrityError(
                "Attachment reference does not belong to this table/record/field"
            )
        canonical.append(_canonical_metadata(registry))

    active_ids = _active_registry_ids_for_cell(
        connection=connection,
        in_session_registry=in_session_registry,
        record=record,
        field_id=field_id,
    )
    if seen != active_ids:
        missing = sorted(active_ids - seen)
        extra = sorted(seen - active_ids)
        detail = []
        if missing:
            detail.append(f"missing active attachment(s): {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected attachment(s): {', '.join(extra)}")
        raise AttachmentReferenceIntegrityError(
            "Attachment field does not match its active registry set"
            + (f" ({'; '.join(detail)})" if detail else "")
        )
    return canonical


@event.listens_for(Session, "before_flush")
def enforce_attachment_record_invariants(session: Session, flush_context, instances) -> None:
    """Make stable attachment ownership a database-write invariant.

    Attachment APIs are not the only code path capable of updating TableRecord JSON.
    Enforcing the registry contract at flush time prevents GraphQL, automation, import,
    template initialization, or future internal writers from persisting presigned URLs,
    cross-row references, or stale lists that silently drop still-active objects.
    """
    session_objects = set(session.new).union(session.dirty)
    records = [obj for obj in session_objects if isinstance(obj, TableRecord)]
    if not records:
        return

    connection = session.connection()
    in_session_registry = {
        str(obj.id): obj
        for obj in session_objects
        if isinstance(obj, AttachmentObject)
    }
    pending_attachment_fields: dict[str, set[str]] = {}
    for obj in session.new:
        if isinstance(obj, TableField) and str(obj.type) == "attachment":
            pending_attachment_fields.setdefault(str(obj.table_id), set()).add(str(obj.id))

    fields_by_table: dict[str, set[str]] = {}
    for record in records:
        table_id = str(record.table_id)
        attachment_fields = fields_by_table.get(table_id)
        if attachment_fields is None:
            attachment_fields = set(
                connection.execute(
                    select(TableField.id).where(
                        TableField.table_id == table_id,
                        TableField.type == "attachment",
                    )
                ).scalars()
            )
            attachment_fields.update(pending_attachment_fields.get(table_id, set()))
            fields_by_table[table_id] = attachment_fields
        if not attachment_fields:
            continue

        changed_fields = _changed_attachment_fields(record, attachment_fields)
        if not changed_fields:
            continue
        next_data = dict(record.data or {})
        changed = False
        for field_id in changed_fields:
            canonical = _validate_attachment_value(
                connection=connection,
                in_session_registry=in_session_registry,
                record=record,
                field_id=field_id,
                value=next_data.get(field_id),
            )
            if next_data.get(field_id) != canonical:
                next_data[field_id] = canonical
                changed = True
        if changed:
            record.data = next_data
