from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AttachmentObject(Base):
    """Durable attachment registry.

    Table record JSON stores only stable attachment metadata. Storage credentials and
    presigned URLs are never persisted. The registry is the authority for attachment
    ownership/scope and for retryable object cleanup after delete/purge.
    """

    __tablename__ = "attachment_objects"

    id = Column(String(64), primary_key=True)
    object_key = Column(String(512), nullable=False, unique=True, index=True)
    table_id = Column(String(128), nullable=False, index=True)
    record_id = Column(String(128), nullable=False, index=True)
    field_id = Column(String(128), nullable=False, index=True)
    filename = Column(String(512), nullable=False)
    size = Column(BigInteger, nullable=False)
    content_type = Column(String(255), nullable=False, default="application/octet-stream")
    created_by_user_id = Column(Integer, nullable=True, index=True)
    status = Column(String(32), nullable=False, default="active", index=True)
    cleanup_attempts = Column(Integer, nullable=False, default=0)
    cleanup_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        index=True,
    )
