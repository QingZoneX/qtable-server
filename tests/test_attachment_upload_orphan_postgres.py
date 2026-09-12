from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from sqlalchemy import select
from starlette.datastructures import Headers
from fastapi import UploadFile

import app.api.attachments as attachments_api
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models.attachment import AttachmentObject
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.attachment_storage import (
    ACTIVE_STATUS,
    UPLOAD_PENDING_STATUS,
    cleanup_pending_attachments,
    put_file as real_put_file,
    stat_object,
)


pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL + MinIO attachment orphan recovery contract",
)


def _upload(name: str, payload: bytes) -> UploadFile:
    return UploadFile(
        BytesIO(payload),
        filename=name,
        headers=Headers({"content-type": "text/plain"}),
    )


@pytest.mark.asyncio
async def test_upload_intent_precedes_minio_write_and_stale_intent_recovers(monkeypatch):
    suffix = uuid.uuid4().hex[:12]
    user_id = 928001
    workspace_id = f"ws-attachment-orphan-{suffix}"
    table_id = f"tbl-attachment-orphan-{suffix}"
    record_id = f"record-attachment-orphan-{suffix}"
    field_id = "files"
    payload = b"QTable MinIO durable upload intent\n"
    observed_intent_id: str | None = None

    async with AsyncSessionLocal() as session:
        user = User(
            id=user_id,
            email=f"attachment-orphan-{suffix}@example.test",
            name="Attachment Orphan Recovery",
            password_hash="!test",
        )
        session.add_all(
            [
                user,
                Workspace(id=workspace_id, name="Attachment orphan recovery"),
                WorkspaceItem(
                    id=table_id,
                    workspace_id=workspace_id,
                    type="table",
                    name="Attachment orphan table",
                    parent_id=None,
                    order_index=0,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                TableField(
                    id=field_id,
                    table_id=table_id,
                    name="Files",
                    type="attachment",
                    order_index=0,
                ),
                TableRecord(
                    id=record_id,
                    table_id=table_id,
                    data={field_id: []},
                    order_index=0,
                    created_by_user_id=user_id,
                    version=1,
                ),
            ]
        )
        await session.commit()

        async def assert_intent_then_write(**kwargs):
            nonlocal observed_intent_id
            object_key = str(kwargs["object_key"])
            attachment_id = object_key.rsplit("/", 1)[-1]
            async with AsyncSessionLocal() as verify_session:
                intent = await verify_session.get(AttachmentObject, attachment_id)
                assert intent is not None
                assert intent.status == UPLOAD_PENDING_STATUS
                assert intent.object_key == object_key
                assert intent.table_id == table_id
                assert intent.record_id == record_id
                assert intent.field_id == field_id
                observed_intent_id = attachment_id
            await real_put_file(**kwargs)

        monkeypatch.setattr(attachments_api, "put_file", assert_intent_then_write)
        result = await attachments_api.upload_attachment(
            table_id=table_id,
            record_id=record_id,
            field_id=field_id,
            file=_upload("durable-intent.txt", payload),
            db=session,
            current_user=user,
        )
        metadata = result["attachment"]
        assert observed_intent_id == metadata["attachmentId"]

        active = await session.get(AttachmentObject, metadata["attachmentId"])
        assert active is not None and active.status == ACTIVE_STATUS
        record_result = await session.execute(
            select(TableRecord).where(
                TableRecord.table_id == table_id,
                TableRecord.id == record_id,
            )
        )
        stored_record = record_result.scalars().one()
        assert stored_record.data[field_id] == [metadata]
        await stat_object(metadata["objectKey"])

        # Recreate the durable state left by a hard process crash after MinIO has
        # accepted the bytes but before the upload intent can be activated in the
        # record transaction. This object is intentionally not referenced by JSON.
        crash_id = uuid.uuid4().hex
        crash_key = f"attachments/{table_id}/{record_id}/{crash_id}"
        crash_payload = b"simulated post-MinIO process crash"
        crash_intent = AttachmentObject(
            id=crash_id,
            object_key=crash_key,
            table_id=table_id,
            record_id=record_id,
            field_id=field_id,
            filename="crash-window.txt",
            size=len(crash_payload),
            content_type="text/plain",
            created_by_user_id=user_id,
            status=UPLOAD_PENDING_STATUS,
        )
        session.add(crash_intent)
        await session.commit()
        await real_put_file(
            object_key=crash_key,
            file_obj=BytesIO(crash_payload),
            size=len(crash_payload),
            content_type="text/plain",
        )
        await stat_object(crash_key)

        # A legitimate in-flight upload is never reclaimed merely because the
        # cleanup worker ran. Only intents older than the configured grace qualify.
        cleaned, pending = await cleanup_pending_attachments(
            session,
            table_id=table_id,
            record_id=record_id,
        )
        assert cleaned == 0
        assert pending == 0
        fresh = await session.get(AttachmentObject, crash_id)
        assert fresh is not None and fresh.status == UPLOAD_PENDING_STATUS
        await stat_object(crash_key)

        grace = max(0.0, float(settings.ATTACHMENT_UPLOAD_PENDING_GRACE_SECONDS))
        fresh.created_at = datetime.now(timezone.utc) - timedelta(seconds=grace + 10)
        await session.commit()

        cleaned, pending = await cleanup_pending_attachments(
            session,
            table_id=table_id,
            record_id=record_id,
        )
        assert cleaned == 1
        assert pending == 0
        assert await session.get(AttachmentObject, crash_id) is None
        with pytest.raises(FileNotFoundError):
            await stat_object(crash_key)
