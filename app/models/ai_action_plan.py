from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from app.db.base import Base


class AiActionPlanBatch(Base):
    __tablename__ = "ai_action_plan_batches"

    id = Column(String(64), primary_key=True)
    trace_id = Column(String(64), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    workspace_id = Column(String(128), nullable=False, index=True)
    diagnosis_id = Column(String(64), nullable=False, index=True)
    table_ids = Column(JSON, nullable=False)
    request_payload = Column(JSON, nullable=False)
    result_payload = Column(JSON, nullable=False)
    record_versions = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default="previewed", index=True)
    change_set_ids = Column(JSON, nullable=False, default=list)
    provider = Column(String(64), nullable=False, default="deterministic-fallback")
    model = Column(String(128), nullable=True)
    apply_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    applied_at = Column(DateTime(timezone=True), nullable=True)
