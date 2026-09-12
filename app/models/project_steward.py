from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from app.db.base import Base


class ProjectStewardDiagnosis(Base):
    __tablename__ = "project_steward_diagnoses"

    id = Column(String(64), primary_key=True)
    trace_id = Column(String(64), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    workspace_id = Column(String(128), nullable=False, index=True)
    project_id = Column(String(128), nullable=True, index=True)
    table_ids = Column(JSON, nullable=False)
    question = Column(Text, nullable=False)
    request_payload = Column(JSON, nullable=False)
    snapshot_fingerprint = Column(String(64), nullable=False, index=True)
    result_payload = Column(JSON, nullable=False)
    provider = Column(String(64), nullable=False, default="deterministic-fallback")
    model = Column(String(128), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
