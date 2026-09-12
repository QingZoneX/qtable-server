from sqlalchemy import Column, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class WorkloadEstimateRun(Base):
    __tablename__ = "workload_estimate_runs"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=True, index=True)
    project_id = Column(String, nullable=True, index=True)
    task_id = Column(String, nullable=True, index=True)
    team_id = Column(String, nullable=True, index=True)
    source_prompt = Column(Text, nullable=False)
    normalized_scope = Column(String(255), nullable=False, index=True)
    business_domain = Column(String(120), nullable=True, index=True)
    quality_bar = Column(String(32), nullable=False, default="production")
    tech_stack_tags = Column(JSON, nullable=True)
    work_type_tags = Column(JSON, nullable=True)
    request_payload = Column(JSON, nullable=False)
    context_summary = Column(JSON, nullable=True)
    historical_summary = Column(JSON, nullable=True)
    structured_output = Column(JSON, nullable=False)
    confidence_score = Column(Float, nullable=False, default=0.0)
    base_story_points = Column(Float, nullable=False, default=0.0)
    adjusted_story_points = Column(Float, nullable=False, default=0.0)
    p50_hours = Column(Float, nullable=False, default=0.0)
    p90_hours = Column(Float, nullable=False, default=0.0)
    risk_coefficient = Column(Float, nullable=False, default=1.0)
    team_capability_factor = Column(Float, nullable=False, default=1.0)
    tech_stack_factor = Column(Float, nullable=False, default=1.0)
    historical_adjustment_factor = Column(Float, nullable=False, default=1.0)
    status = Column(String(32), nullable=False, default="estimated")
    provider = Column(String(64), nullable=True)
    model = Column(String(120), nullable=True)
    trace_id = Column(String(64), nullable=True, index=True)
    created_by = Column(String(64), nullable=True)
    actual_story_points = Column(Float, nullable=True)
    actual_hours = Column(Float, nullable=True)
    outcome_status = Column(String(32), nullable=True)
    accuracy_rating = Column(Integer, nullable=True)
    feedback_notes = Column(Text, nullable=True)
    feedback_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
