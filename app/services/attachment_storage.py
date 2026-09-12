from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable, Optional

from minio import Minio
from minio.error import S3Error
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.attachment import AttachmentObject
from app.models.change_history import RecycleBinRecord
from app.models.smart_table import TableRecord

logger = logging.getLogger(__name__)

ACTIVE_STATUS = "active"
UPLOAD_PENDING_STATUS = "upload_pending"
DELETE_PENDING_STATUS = "delete_pending"
PURGE_PENDING_STATUS = "purge_pending"
PENDING_STATUSES = (DELETE_PENDING_STATUS, PURGE_PENDING_STATUS)


class AttachmentStorageError(RuntimeError):
    pass


def _client() -> Minio:
    endpoint = (settings.ATTACHMENT_S3_ENDPOINT or "").strip()
    access_key = (settings.ATTACHMENT_S3_ACCESS_KEY or "").strip()
    secret_key = settings.ATTACHMENT_S3_SECRET_KEY or ""
    region = (settings.ATTACHMENT_S3_REGION or "").strip() or None
    if not endpoint or not access_key or not secret_key:
        raise AttachmentStorageError("Attachment storage is not configured")
    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=bool(settings.ATTACHMENT_S3_SECURE),
        region=region,
    )


def attachment_metadata(obj: AttachmentObject) -> dict[str, Any]:
    return {
        "attachmentId": obj.id,
        "objectKey": obj.object_key,
        "name": obj.filename,
        "size": int(obj.size or 0),
        "contentType": obj.content_type or "application/octet-stream",
    }


def stable_attachment_list(value: Any) -> list[dict[str, Any]]:
    """Return only stable attachment references.

    Legacy values that only contain expiring `url` fields cannot be made secure or
    durable after the fact, so they are intentionally not treated as live objects.
    """
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        attachment_id = item.get("attachmentId")
        object_key = item.get("objectKey")
        if not isinstance(attachment_id, str) or not attachment_id:
            continue
        if not isinstance(object_key, str) or not object_key:
            continue
        result.append(
            {
                "attachmentId": attachment_id,
                "objectKey": object_key,
                "name": str(item.get("name") or "attachment"),
                "size": int(item.get("size") or 0),
                "contentType": str(
                    item.get("contentType") or "application/octet-stream"
                ),
            }
        )
    return result


async def canonicalize_attachment_list(
    db: AsyncSession,
    *,
    table_id: str,
    record_id: str,
    field_id: str,
    value: Any,
) -> list[dict[str, Any]]:
    """Resolve record JSON through the registry and reject cross-scope references."""
    requested = stable_attachment_list(value)
    if not requested:
        return []
    ids = [item["attachmentId"] for item in requested]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate attachment reference")

    result = await db.execute(
        select(AttachmentObject).where(
            AttachmentObject.id.in_(ids),
            AttachmentObject.status == ACTIVE_STATUS,
        )
    )
    by_id = {obj.id: obj for obj in result.scalars().all()}
    canonical: list[dict[str, Any]] = []
    for requested_item in requested:
        obj = by_id.get(requested_item["attachmentId"])
        if obj is None:
            raise ValueError("Attachment reference is missing or inactive")
        if (
            obj.table_id != table_id
            or obj.record_id != str(record_id)
            or obj.field_id != field_id
            or obj.object_key != requested_item["objectKey"]
        ):
            raise ValueError("Attachment reference does not belong to this cell")
        canonical.append(attachment_metadata(obj))
    return canonical


async def ensure_bucket() -> None:
    if not settings.ATTACHMENT_STORAGE_ENABLED:
        raise AttachmentStorageError("Attachment storage is disabled")
    client = _client()
    bucket = settings.ATTACHMENT_S3_BUCKET
    try:
        exists = await asyncio.to_thread(client.bucket_exists, bucket)
        if not exists:
            try:
                await asyncio.to_thread(client.make_bucket, bucket)
            except S3Error as exc:
                # Concurrent startup/upload may have created the bucket first.
                if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise
    except AttachmentStorageError:
        raise
    except Exception as exc:  # pragma: no cover - exact SDK transport error varies
        raise AttachmentStorageError("Attachment storage is unavailable") from exc


async def put_file(*, object_key: str, file_obj: Any, size: int, content_type: str) -> None:
    await ensure_bucket()
    client = _client()
    try:
        await asyncio.to_thread(
            client.put_object,
            settings.ATTACHMENT_S3_BUCKET,
            object_key,
            file_obj,
            size,
            content_type=content_type,
        )
    except Exception as exc:
        raise AttachmentStorageError("Attachment upload failed") from exc


async def stat_object(object_key: str) -> None:
    """Verify the object is readable before a StreamingResponse commits headers."""
    client = _client()
    try:
        await asyncio.to_thread(
            client.stat_object,
            settings.ATTACHMENT_S3_BUCKET,
            object_key,
        )
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NotFound"}:
            raise FileNotFoundError(object_key) from exc
        raise AttachmentStorageError("Attachment download failed") from exc
    except Exception as exc:
        raise AttachmentStorageError("Attachment download failed") from exc


async def remove_object(object_key: str) -> None:
    client = _client()
    try:
        await asyncio.to_thread(
            client.remove_object,
            settings.ATTACHMENT_S3_BUCKET,
            object_key,
        )
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NotFound"}:
            return
        raise AttachmentStorageError("Attachment cleanup failed") from exc
    except Exception as exc:
        raise AttachmentStorageError("Attachment cleanup failed") from exc


async def stream_object(object_key: str) -> AsyncIterator[bytes]:
    client = _client()
    try:
        response = await asyncio.to_thread(
            client.get_object,
            settings.ATTACHMENT_S3_BUCKET,
            object_key,
        )
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NotFound"}:
            raise FileNotFoundError(object_key) from exc
        raise AttachmentStorageError("Attachment download failed") from exc
    except Exception as exc:
        raise AttachmentStorageError("Attachment download failed") from exc

    try:
        while True:
            chunk = await asyncio.to_thread(response.read, 64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        await asyncio.to_thread(response.close)
        await asyncio.to_thread(response.release_conn)


async def mark_record_attachments_for_purge(
    db: AsyncSession, *, table_id: str, record_id: str
) -> int:
    result = await db.execute(
        select(AttachmentObject).where(
            AttachmentObject.table_id == table_id,
            AttachmentObject.record_id == str(record_id),
            AttachmentObject.status == ACTIVE_STATUS,
        )
    )
    objects = result.scalars().all()
    for obj in objects:
        obj.status = PURGE_PENDING_STATUS
        obj.cleanup_error = None
    return len(objects)


async def reconcile_purged_record_attachments(
    db: AsyncSession, *, limit: Optional[int] = None
) -> int:
    """Mark objects whose record was permanently purged for cleanup.

    A soft-deleted record has no live TableRecord row but still has a `deleted`
    recycle snapshot, so its objects remain active and can be restored. Once purge
    removes that snapshot, an active registry row with neither live record nor
    deleted recycle snapshot is an orphan and must be cleaned. The orphan filter is
    applied before LIMIT so a large set of older live objects cannot starve cleanup.
    """
    live_record_exists = (
        select(TableRecord.id)
        .where(
            TableRecord.table_id == AttachmentObject.table_id,
            TableRecord.id == AttachmentObject.record_id,
        )
        .exists()
    )
    deleted_recycle_exists = (
        select(RecycleBinRecord.id)
        .where(
            RecycleBinRecord.table_id == AttachmentObject.table_id,
            RecycleBinRecord.record_id == AttachmentObject.record_id,
            RecycleBinRecord.status == "deleted",
        )
        .exists()
    )
    result = await db.execute(
        select(AttachmentObject)
        .where(
            AttachmentObject.status == ACTIVE_STATUS,
            ~live_record_exists,
            ~deleted_recycle_exists,
        )
        .order_by(AttachmentObject.created_at.asc())
        .limit(limit or settings.ATTACHMENT_CLEANUP_BATCH_SIZE)
    )
    objects = result.scalars().all()
    for obj in objects:
        obj.status = PURGE_PENDING_STATUS
        obj.cleanup_error = None
    if objects:
        await db.commit()
    return len(objects)


async def cleanup_attachment_object(db: AsyncSession, obj: AttachmentObject) -> bool:
    try:
        await remove_object(obj.object_key)
    except AttachmentStorageError as exc:
        obj.cleanup_attempts = int(obj.cleanup_attempts or 0) + 1
        obj.cleanup_error = str(exc)
        await db.commit()
        logger.warning(
            "Attachment cleanup pending id=%s key=%s attempts=%s",
            obj.id,
            obj.object_key,
            obj.cleanup_attempts,
        )
        return False
    await db.delete(obj)
    await db.commit()
    return True


async def cleanup_pending_attachments(
    db: AsyncSession,
    *,
    table_id: Optional[str] = None,
    record_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> tuple[int, int]:
    """Clean retryable deletes/purges and abandoned upload intents.

    Upload intents are deliberately not immediately eligible. A request commits its
    intent before sending bytes to object storage, so the worker only treats an
    `upload_pending` row as abandoned after a configurable grace window. This closes
    the process-crash orphan window without racing a legitimate slow upload.
    """
    grace_seconds = max(
        0.0,
        float(settings.ATTACHMENT_UPLOAD_PENDING_GRACE_SECONDS),
    )
    upload_cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
    eligible_status = or_(
        AttachmentObject.status.in_(PENDING_STATUSES),
        and_(
            AttachmentObject.status == UPLOAD_PENDING_STATUS,
            AttachmentObject.created_at <= upload_cutoff,
        ),
    )
    stmt = select(AttachmentObject).where(eligible_status)
    if table_id is not None:
        stmt = stmt.where(AttachmentObject.table_id == table_id)
    if record_id is not None:
        stmt = stmt.where(AttachmentObject.record_id == str(record_id))
    stmt = stmt.order_by(AttachmentObject.created_at.asc())
    stmt = stmt.limit(limit or settings.ATTACHMENT_CLEANUP_BATCH_SIZE)
    result = await db.execute(stmt)
    objects: Iterable[AttachmentObject] = result.scalars().all()
    cleaned = 0
    pending = 0
    for obj in objects:
        if await cleanup_attachment_object(db, obj):
            cleaned += 1
        else:
            pending += 1
    return cleaned, pending
