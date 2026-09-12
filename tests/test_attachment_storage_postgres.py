from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from starlette.datastructures import Headers

from app.api.attachments import delete_attachment, download_attachment, upload_attachment
from app.db.session import AsyncSessionLocal, engine
from app.models.attachment import AttachmentObject
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    WorkspaceItem,
)
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.attachment_invariants import AttachmentReferenceIntegrityError
from app.services.attachment_storage import (
    ACTIVE_STATUS,
    canonicalize_attachment_list,
    cleanup_pending_attachments,
    reconcile_purged_record_attachments,
    remove_object,
)
from app.services.change_history import purge_recycled_record, restore_recycled_record
from app.services.smart_table_store.db_backend import delete_record, update_record


pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL + S3 attachment integration contract",
)


def _upload(name: str, payload: bytes, content_type: str = "text/plain") -> UploadFile:
    return UploadFile(
        BytesIO(payload),
        filename=name,
        headers=Headers({"content-type": content_type}),
    )


async def _response_bytes(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_attachment_storage_permission_contract_and_recycle_restore_purge_lifecycle():
    alice_id = 926101
    bob_id = 926102
    workspace_id = "ws-attachment-pg"
    table_id = "tbl-attachment-pg"
    alice_record_id = "record-attachment-alice"
    bob_record_id = "record-attachment-bob"
    concurrent_record_id = "record-attachment-concurrent"
    attachment_field_id = "files"

    async with AsyncSessionLocal() as session:
        alice = User(
            id=alice_id,
            email="attachment-pg-alice@example.test",
            name="Attachment PG Alice",
            password_hash="!test",
        )
        bob = User(
            id=bob_id,
            email="attachment-pg-bob@example.test",
            name="Attachment PG Bob",
            password_hash="!test",
        )
        session.add_all(
            [
                alice,
                bob,
                Workspace(id=workspace_id, name="Attachment PostgreSQL"),
                WorkspaceItem(
                    id=table_id,
                    workspace_id=workspace_id,
                    type="table",
                    name="Attachment table",
                    parent_id=None,
                    order_index=0,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=alice_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=bob_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                TableField(
                    id="owner",
                    table_id=table_id,
                    name="Owner",
                    type="member",
                    property={"multiple": True},
                    order_index=0,
                ),
                TableField(
                    id=attachment_field_id,
                    table_id=table_id,
                    name="Files",
                    type="attachment",
                    order_index=1,
                ),
                TableRowPermissionPolicy(
                    table_id=table_id,
                    mode="member_field",
                    member_field_id="owner",
                ),
                TableRecord(
                    id=alice_record_id,
                    table_id=table_id,
                    data={"owner": [str(alice_id)], attachment_field_id: []},
                    order_index=0,
                    created_by_user_id=alice_id,
                    version=1,
                ),
                TableRecord(
                    id=bob_record_id,
                    table_id=table_id,
                    data={"owner": [str(bob_id)], attachment_field_id: []},
                    order_index=1,
                    created_by_user_id=bob_id,
                    version=1,
                ),
                TableRecord(
                    id=concurrent_record_id,
                    table_id=table_id,
                    data={"owner": [str(alice_id)], attachment_field_id: []},
                    order_index=2,
                    created_by_user_id=alice_id,
                    version=1,
                ),
            ]
        )
        await session.commit()

        # Real S3-compatible upload. The record stores only stable registry data,
        # never an expiring presigned URL.
        payload = b"QTable durable attachment\n"
        uploaded = await upload_attachment(
            table_id=table_id,
            record_id=alice_record_id,
            field_id=attachment_field_id,
            file=_upload("release-gate.txt", payload),
            db=session,
            current_user=alice,
        )
        metadata = uploaded["attachment"]
        assert set(metadata) == {
            "attachmentId",
            "objectKey",
            "name",
            "size",
            "contentType",
        }
        assert "url" not in metadata
        assert metadata["objectKey"].endswith(metadata["attachmentId"])
        assert uploaded["attachments"] == [metadata]

        record_result = await session.execute(
            select(TableRecord).where(
                TableRecord.table_id == table_id,
                TableRecord.id == alice_record_id,
            )
        )
        stored_record = record_result.scalars().one()
        assert stored_record.data[attachment_field_id] == [metadata]

        # Two independent transactions may upload to the same cell at once. The
        # row lock must be acquired before canonicalising the current attachment
        # list so neither committed object is lost by a stale read/whole-array write.
        async def concurrent_upload(name: str, payload_bytes: bytes):
            async with AsyncSessionLocal() as concurrent_session:
                actor = await concurrent_session.get(User, alice_id)
                assert actor is not None
                return await upload_attachment(
                    table_id=table_id,
                    record_id=concurrent_record_id,
                    field_id=attachment_field_id,
                    file=_upload(name, payload_bytes),
                    db=concurrent_session,
                    current_user=actor,
                )

        first_concurrent, second_concurrent = await asyncio.gather(
            concurrent_upload("concurrent-a.txt", b"concurrent A"),
            concurrent_upload("concurrent-b.txt", b"concurrent B"),
        )
        concurrent_items = {
            first_concurrent["attachment"]["attachmentId"]: first_concurrent["attachment"],
            second_concurrent["attachment"]["attachmentId"]: second_concurrent["attachment"],
        }
        assert len(concurrent_items) == 2

        async with AsyncSessionLocal() as verify_session:
            concurrent_record = await verify_session.get(
                TableRecord,
                (concurrent_record_id, table_id),
            )
            if concurrent_record is None:
                concurrent_record_result = await verify_session.execute(
                    select(TableRecord).where(
                        TableRecord.table_id == table_id,
                        TableRecord.id == concurrent_record_id,
                    )
                )
                concurrent_record = concurrent_record_result.scalars().one()
            stored_concurrent = concurrent_record.data[attachment_field_id]
            assert {item["attachmentId"] for item in stored_concurrent} == set(concurrent_items)

            # A stale generic grid/GraphQL write that omits a still-ACTIVE object
            # must fail at the ORM boundary instead of orphaning its registry/S3 data.
            with pytest.raises(
                AttachmentReferenceIntegrityError,
                match="active registry set",
            ):
                await update_record(
                    verify_session,
                    table_id,
                    concurrent_record_id,
                    attachment_field_id,
                    [next(iter(concurrent_items.values()))],
                    user_id=alice_id,
                    source="stale_generic_attachment_write",
                )
            await verify_session.rollback()

        async def concurrent_delete(attachment_id: str):
            async with AsyncSessionLocal() as concurrent_session:
                actor = await concurrent_session.get(User, alice_id)
                assert actor is not None
                return await delete_attachment(
                    attachment_id,
                    db=concurrent_session,
                    current_user=actor,
                )

        deleted_concurrently = await asyncio.gather(
            *(concurrent_delete(attachment_id) for attachment_id in concurrent_items)
        )
        assert all(item["deleted"] is True for item in deleted_concurrently)
        assert all(item["storageCleanup"] == "complete" for item in deleted_concurrently)
        async with AsyncSessionLocal() as verify_session:
            concurrent_record_result = await verify_session.execute(
                select(TableRecord).where(
                    TableRecord.table_id == table_id,
                    TableRecord.id == concurrent_record_id,
                )
            )
            concurrent_record = concurrent_record_result.scalars().one()
            assert concurrent_record.data[attachment_field_id] == []
            remaining_registry = await verify_session.execute(
                select(AttachmentObject).where(
                    AttachmentObject.id.in_(list(concurrent_items))
                )
            )
            assert remaining_registry.scalars().all() == []

        # Download goes through the bound record's current permission on every
        # request. No presigned URL bypass exists.
        download = await download_attachment(
            metadata["attachmentId"], db=session, current_user=alice
        )
        assert download.headers["cache-control"] == "private, no-store"
        assert await _response_bytes(download) == payload

        with pytest.raises(HTTPException) as bob_download:
            await download_attachment(
                metadata["attachmentId"], db=session, current_user=bob
            )
        assert bob_download.value.status_code == 404

        with pytest.raises(HTTPException) as bob_delete:
            await delete_attachment(
                metadata["attachmentId"], db=session, current_user=bob
            )
        assert bob_delete.value.status_code == 404

        # A stable id/objectKey copied into another cell cannot be canonicalized;
        # attachment registry scope is table + record + field, not possession of a key.
        with pytest.raises(ValueError, match="does not belong"):
            await canonicalize_attachment_list(
                session,
                table_id=table_id,
                record_id=bob_record_id,
                field_id=attachment_field_id,
                value=[metadata],
            )

        # The same rule is enforced below the attachment API. A generic record
        # mutation cannot persist a cross-row reference or revive legacy URL data.
        with pytest.raises(AttachmentReferenceIntegrityError, match="does not belong"):
            await update_record(
                session,
                table_id,
                bob_record_id,
                attachment_field_id,
                [metadata],
                user_id=bob_id,
                source="generic_write_bypass_test",
            )
        await session.rollback()

        with pytest.raises(AttachmentReferenceIntegrityError, match="URLs cannot be persisted"):
            await update_record(
                session,
                table_id,
                alice_record_id,
                attachment_field_id,
                [
                    {
                        "attachmentId": "legacy",
                        "objectKey": "legacy-object",
                        "name": "legacy.txt",
                        "url": "https://example.test/expires-soon",
                    }
                ],
                user_id=alice_id,
                source="generic_write_bypass_test",
            )
        await session.rollback()

        # Rollback correctly expires ORM state. Reload actors explicitly rather
        # than accidentally testing a sync lazy-load on the asyncpg driver.
        alice = await session.get(User, alice_id)
        bob = await session.get(User, bob_id)
        assert alice is not None and bob is not None

        # Caller-controlled content-type is bounded/sanitized. Registry/object
        # divergence is also a real 404 before streaming headers are committed.
        missing = await upload_attachment(
            table_id=table_id,
            record_id=alice_record_id,
            field_id=attachment_field_id,
            file=_upload("missing-object.bin", b"missing", "not a mime type"),
            db=session,
            current_user=alice,
        )
        missing_metadata = missing["attachment"]
        assert missing_metadata["contentType"] == "application/octet-stream"
        await remove_object(missing_metadata["objectKey"])
        with pytest.raises(HTTPException) as missing_download:
            await download_attachment(
                missing_metadata["attachmentId"], db=session, current_user=alice
            )
        assert missing_download.value.status_code == 404
        missing_deleted = await delete_attachment(
            missing_metadata["attachmentId"], db=session, current_user=alice
        )
        assert missing_deleted["deleted"] is True
        assert missing_deleted["storageCleanup"] == "complete"

        # Explicit delete closes both record metadata and object cleanup.
        disposable = await upload_attachment(
            table_id=table_id,
            record_id=alice_record_id,
            field_id=attachment_field_id,
            file=_upload("delete-me.txt", b"delete me"),
            db=session,
            current_user=alice,
        )
        deleted = await delete_attachment(
            disposable["attachment"]["attachmentId"],
            db=session,
            current_user=alice,
        )
        assert deleted["deleted"] is True
        assert deleted["storageCleanup"] == "complete"
        assert deleted["attachments"] == [metadata]
        deleted_registry = await session.get(
            AttachmentObject, disposable["attachment"]["attachmentId"]
        )
        assert deleted_registry is None

        # Recycle keeps object data live so restore is a real restoration, not a
        # metadata-only row recovery with a broken file.
        assert await delete_record(
            session,
            table_id,
            alice_record_id,
            user_id=alice_id,
            source="attachment_release_gate",
        )
        assert await reconcile_purged_record_attachments(session) == 0
        registry = await session.get(AttachmentObject, metadata["attachmentId"])
        assert registry is not None and registry.status == ACTIVE_STATUS

        restored = await restore_recycled_record(
            session,
            table_id=table_id,
            record_id=alice_record_id,
            actor_id=alice_id,
        )
        assert restored is not None
        restored_download = await download_attachment(
            metadata["attachmentId"], db=session, current_user=alice
        )
        assert await _response_bytes(restored_download) == payload

        # Permanent purge removes history/recycle state in the existing service;
        # reconciliation then durably marks and physically removes the S3 object.
        assert await delete_record(
            session,
            table_id,
            alice_record_id,
            user_id=alice_id,
            source="attachment_release_gate",
        )
        assert await purge_recycled_record(
            session,
            table_id=table_id,
            record_id=alice_record_id,
            actor_id=alice_id,
        )
        assert await reconcile_purged_record_attachments(session) == 1
        cleaned, pending = await cleanup_pending_attachments(session)
        assert cleaned >= 1
        assert pending == 0
        assert await session.get(AttachmentObject, metadata["attachmentId"]) is None
