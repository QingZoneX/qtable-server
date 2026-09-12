from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.models.attachment import AttachmentObject
from app.models.smart_table import TableField, TableRecord
from app.models.user import User
from app.services.attachment_storage import (
    ACTIVE_STATUS,
    DELETE_PENDING_STATUS,
    UPLOAD_PENDING_STATUS,
    AttachmentStorageError,
    attachment_metadata,
    canonicalize_attachment_list,
    cleanup_attachment_object,
    put_file,
    stable_attachment_list,
    stat_object,
    stream_object,
)
from app.services.row_permissions import (
    get_row_permission_policy,
    record_is_visible,
    require_record_access,
)
from app.services.smart_table_store.db_backend import update_record
from app.services.workspace.permissions import (
    get_effective_permission_for_item,
    permission_allows,
)

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

_SAFE_FILENAME_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_MIME_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


def _safe_filename(filename: str | None) -> str:
    name = Path(filename or "").name.strip()
    name = _SAFE_FILENAME_RE.sub("_", name)
    if not name:
        return "attachment"
    return name[:512]


def _safe_content_type(content_type: str | None) -> str:
    # Content-Type is caller-controlled metadata. Keep only a bounded base MIME
    # token so it cannot inject response headers or overflow the registry column.
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if not base or len(base) > 255 or not _SAFE_MIME_RE.fullmatch(base):
        return "application/octet-stream"
    return base


def _record_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Record not found or no access",
    )


async def _require_scoped_record(
    db: AsyncSession,
    *,
    user: User,
    table_id: str,
    record_id: str,
    required_permission: str,
) -> TableRecord:
    table_permission = await get_effective_permission_for_item(db, user.id, table_id)
    if not permission_allows(table_permission, required_permission):
        raise _record_not_found()
    try:
        return await require_record_access(
            db,
            table_id=table_id,
            record_id=str(record_id),
            user_id=user.id,
            table_permission=table_permission,
        )
    except PermissionError as exc:
        raise _record_not_found() from exc


async def _lock_scoped_record_for_attachment_mutation(
    db: AsyncSession,
    *,
    user: User,
    table_id: str,
    record_id: str,
) -> TableRecord:
    """Lock a row and re-check its current permission before mutating attachments.

    Attachment endpoints update the complete attachment list for one cell. Reading
    that list before acquiring the row lock permits concurrent uploads/deletes to
    overwrite each other. A cheap permission pre-check avoids locking hidden rows;
    the permission is then checked again against the freshly locked row because the
    member/creator policy data may have changed while this request was waiting.
    """
    await _require_scoped_record(
        db,
        user=user,
        table_id=table_id,
        record_id=record_id,
        required_permission="update",
    )
    result = await db.execute(
        select(TableRecord)
        .where(
            TableRecord.table_id == table_id,
            TableRecord.id == str(record_id),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    record = result.scalars().first()
    if record is None:
        raise _record_not_found()

    table_permission = await get_effective_permission_for_item(db, user.id, table_id)
    if not permission_allows(table_permission, "update"):
        raise _record_not_found()
    policy = await get_row_permission_policy(db, table_id)
    if not record_is_visible(
        policy=policy,
        user_id=user.id,
        table_permission=table_permission,
        created_by_user_id=record.created_by_user_id,
        data=dict(record.data or {}),
    ):
        raise _record_not_found()
    return record


async def _require_attachment_field(
    db: AsyncSession, *, table_id: str, field_id: str
) -> TableField:
    result = await db.execute(
        select(TableField).where(
            TableField.table_id == table_id,
            TableField.id == field_id,
        )
    )
    field = result.scalars().first()
    if field is None or str(field.type) != "attachment":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Attachment operations require an attachment field",
        )
    return field


def _file_size(upload: UploadFile) -> int:
    stream = upload.file
    current = stream.tell()
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(current)
    return int(size)


async def _load_active_attachment(
    db: AsyncSession, attachment_id: str
) -> AttachmentObject:
    result = await db.execute(
        select(AttachmentObject).where(
            AttachmentObject.id == attachment_id,
            AttachmentObject.status == ACTIVE_STATUS,
        )
    )
    obj = result.scalars().first()
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    return obj


async def _cleanup_failed_upload_intent(db: AsyncSession, attachment_id: str) -> None:
    """Best-effort immediate cleanup while preserving a durable retry marker.

    The upload intent is committed before any S3 write. Rolling back an unsuccessful
    activation therefore restores that durable `upload_pending` row. Mark it for
    immediate delete cleanup; if object storage is unavailable, the existing cleanup
    helper commits the retry state instead of losing the only record of the object.
    """
    await db.rollback()
    result = await db.execute(
        select(AttachmentObject)
        .where(AttachmentObject.id == attachment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    intent = result.scalars().first()
    if intent is None:
        return
    intent.status = DELETE_PENDING_STATUS
    intent.cleanup_error = None
    await cleanup_attachment_object(db, intent)


@router.post("/tables/{table_id}/records/{record_id}/fields/{field_id}")
async def upload_attachment(
    table_id: str,
    record_id: str,
    field_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if not settings.ATTACHMENT_STORAGE_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Attachment storage is disabled",
        )

    # Reject unauthorised requests before spending storage bandwidth. Permission
    # is checked again after the S3 write while holding the row lock.
    await _require_scoped_record(
        db,
        user=current_user,
        table_id=table_id,
        record_id=record_id,
        required_permission="update",
    )
    await _require_attachment_field(db, table_id=table_id, field_id=field_id)

    size = _file_size(file)
    if size > settings.ATTACHMENT_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.ATTACHMENT_MAX_BYTES} byte attachment limit",
        )
    await file.seek(0)

    filename = _safe_filename(file.filename)
    content_type = _safe_content_type(file.content_type)
    attachment_id = uuid.uuid4().hex
    object_key = f"attachments/{table_id}/{record_id}/{attachment_id}"

    # Persist the upload intent BEFORE object storage sees bytes. If the process is
    # killed after this commit, the background worker can identify and eventually
    # remove the abandoned object (or the intent itself if no object was written).
    intent = AttachmentObject(
        id=attachment_id,
        object_key=object_key,
        table_id=table_id,
        record_id=str(record_id),
        field_id=field_id,
        filename=filename,
        size=size,
        content_type=content_type,
        created_by_user_id=current_user.id,
        status=UPLOAD_PENDING_STATUS,
    )
    db.add(intent)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Attachment upload could not create a durable upload intent",
        ) from exc

    try:
        await put_file(
            object_key=object_key,
            file_obj=file.file,
            size=size,
            content_type=content_type,
        )
    except AttachmentStorageError as exc:
        try:
            await _cleanup_failed_upload_intent(db, attachment_id)
        except Exception:
            # The committed upload_pending marker remains durable if immediate
            # cleanup cannot complete; the grace-based worker will retry it.
            await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    try:
        record = await _lock_scoped_record_for_attachment_mutation(
            db,
            user=current_user,
            table_id=table_id,
            record_id=record_id,
        )
        await _require_attachment_field(db, table_id=table_id, field_id=field_id)
        try:
            current = await canonicalize_attachment_list(
                db,
                table_id=table_id,
                record_id=record_id,
                field_id=field_id,
                value=(record.data or {}).get(field_id),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Existing attachment references are inconsistent",
            ) from exc

        # Lock the durable intent only after the record lock, preserving the same
        # record->registry lock order used by delete. Activation and record JSON are
        # then committed atomically by update_record().
        intent_result = await db.execute(
            select(AttachmentObject)
            .where(
                AttachmentObject.id == attachment_id,
                AttachmentObject.status == UPLOAD_PENDING_STATUS,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        intent = intent_result.scalars().first()
        if intent is None:
            raise RuntimeError("Attachment upload intent disappeared before activation")
        intent.status = ACTIVE_STATUS
        intent.cleanup_error = None
        metadata = attachment_metadata(intent)
        updated = await update_record(
            db,
            table_id,
            str(record_id),
            field_id,
            [*current, metadata],
            user_id=current_user.id,
            source="attachment_upload",
        )
        if updated is None:
            raise RuntimeError("Record disappeared during attachment upload")
    except Exception as exc:
        try:
            await _cleanup_failed_upload_intent(db, attachment_id)
        except Exception:
            # The pre-S3 committed upload_pending marker survives rollback and is
            # sufficient for the stale-intent worker to recover after a crash/outage.
            await db.rollback()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attachment could not be committed to the record",
        ) from exc

    return {
        "attachment": metadata,
        "attachments": stable_attachment_list(updated.get(field_id)),
    }


@router.get("/{attachment_id}")
async def download_attachment(
    attachment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    obj = await _load_active_attachment(db, attachment_id)
    await _require_scoped_record(
        db,
        user=current_user,
        table_id=obj.table_id,
        record_id=obj.record_id,
        required_permission="read",
    )
    try:
        await stat_object(obj.object_key)
    except FileNotFoundError as exc:
        # Registry/object divergence is not exposed as a successful empty stream.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment object not found",
        ) from exc
    except AttachmentStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    filename_ascii = obj.filename.encode("ascii", "ignore").decode("ascii") or "attachment"
    disposition = (
        f'attachment; filename="{filename_ascii.replace(chr(34), "")}"; '
        f"filename*=UTF-8''{quote(obj.filename)}"
    )
    return StreamingResponse(
        stream_object(obj.object_key),
        media_type=obj.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{attachment_id}")
async def delete_attachment(
    attachment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    obj = await _load_active_attachment(db, attachment_id)
    await _require_scoped_record(
        db,
        user=current_user,
        table_id=obj.table_id,
        record_id=obj.record_id,
        required_permission="update",
    )
    await _require_attachment_field(db, table_id=obj.table_id, field_id=obj.field_id)

    record = await _lock_scoped_record_for_attachment_mutation(
        db,
        user=current_user,
        table_id=obj.table_id,
        record_id=obj.record_id,
    )
    # Refresh and lock the registry row after the record lock. Two concurrent
    # deletes of the same attachment cannot both operate on a stale ACTIVE object.
    obj_result = await db.execute(
        select(AttachmentObject)
        .where(
            AttachmentObject.id == attachment_id,
            AttachmentObject.status == ACTIVE_STATUS,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    obj = obj_result.scalars().first()
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    await _require_attachment_field(db, table_id=obj.table_id, field_id=obj.field_id)

    try:
        current = await canonicalize_attachment_list(
            db,
            table_id=obj.table_id,
            record_id=obj.record_id,
            field_id=obj.field_id,
            value=(record.data or {}).get(obj.field_id),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attachment references are inconsistent",
        ) from exc
    if attachment_id not in {item["attachmentId"] for item in current}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attachment is not referenced by its bound cell",
        )

    remaining = [item for item in current if item["attachmentId"] != attachment_id]
    obj.status = DELETE_PENDING_STATUS
    obj.cleanup_error = None
    updated = await update_record(
        db,
        obj.table_id,
        obj.record_id,
        obj.field_id,
        remaining,
        user_id=current_user.id,
        source="attachment_delete",
    )
    if updated is None:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record disappeared during attachment delete",
        )

    cleaned = await cleanup_attachment_object(db, obj)
    return {
        "deleted": True,
        "storageCleanup": "complete" if cleaned else "pending",
        "attachments": stable_attachment_list(updated.get(obj.field_id)),
    }
