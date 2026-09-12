from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint

from app.db.base import Base


class ClipperTaskReceipt(Base):
    """Durable receipt for cross-service task creation.

    QNote may retry after a timeout.  The receipt gives QTable an authoritative,
    database-backed idempotency boundary instead of relying on a table scan or
    treating an annotation as if it could only ever create one task.
    """

    __tablename__ = "clipper_task_receipts"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "client_mutation_id",
            name="uq_clipper_receipt_user_mutation",
        ),
    )

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    client_mutation_id = Column(String(128), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    response = Column(JSON, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
