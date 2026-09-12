from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from app.db.base import Base


class MemberAssignmentBatch(Base):
    __tablename__ = "member_assignment_batches"

    id = Column(String(64), primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    workspace_id = Column(String(128), nullable=False, index=True)
    table_id = Column(String(128), nullable=False, index=True)
    record_ids = Column(JSON, nullable=False)
    request_payload = Column(JSON, nullable=False)
    result_payload = Column(JSON, nullable=False)
    record_versions = Column(JSON, nullable=False)
    member_snapshot = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default="previewed", index=True)
    change_set_ids = Column(JSON, nullable=False, default=list)
    apply_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    applied_at = Column(DateTime(timezone=True), nullable=True)
