from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class TaskSplitPlan(Base):
    __tablename__ = "task_split_plans"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=True, index=True)
    project_id = Column(String, nullable=True, index=True)
    task_id = Column(String, nullable=True, index=True)
    source_prompt = Column(Text, nullable=False)
    normalized_goal = Column(String(255), nullable=False)
    summary = Column(Text, nullable=False, default="")
    mermaid = Column(Text, nullable=False, default="")
    context_summary = Column(JSON, nullable=True)
    structured_output = Column(JSON, nullable=False)
    provider = Column(String(64), nullable=True)
    model = Column(String(120), nullable=True)
    trace_id = Column(String(64), nullable=True, index=True)
    created_by = Column(String(64), nullable=True)
    auto_created = Column(Boolean, nullable=False, default=True)
    target_table_id = Column(String, nullable=True, index=True)
    source_fingerprint = Column(String(64), nullable=True, index=True)
    source_reference = Column(JSON, nullable=True)
    selected_record_ids = Column(JSON, nullable=True)
    application_status = Column(String(32), nullable=False, default="previewed", index=True)
    edited_output = Column(JSON, nullable=True)
    applied_records = Column(JSON, nullable=True)
    apply_error = Column(Text, nullable=True)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TaskSplitNode(Base):
    __tablename__ = "task_split_nodes"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    plan_id = Column(String, nullable=False, index=True)
    parent_id = Column(String, nullable=True, index=True)
    node_key = Column(String(64), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False, default="")
    objective = Column(Text, nullable=False, default="")
    depth = Column(Integer, nullable=False, default=0)
    sort_order = Column(Integer, nullable=False, default=0)
    estimate_optimistic_hours = Column(Float, nullable=False, default=0.0)
    estimate_likely_hours = Column(Float, nullable=False, default=0.0)
    estimate_pessimistic_hours = Column(Float, nullable=False, default=0.0)
    estimate_buffered_hours = Column(Float, nullable=False, default=0.0)
    risk_level = Column(String(16), nullable=False, default="medium")
    risk_summary = Column(Text, nullable=False, default="")
    priority = Column(String(16), nullable=False, default="medium")
    milestone = Column(String(255), nullable=False, default="")
    suggested_role = Column(String(255), nullable=False, default="")
    source_reference = Column(JSON, nullable=True)
    deliverables = Column(JSON, nullable=True)
    acceptance_criteria = Column(JSON, nullable=True)
    required_skills = Column(JSON, nullable=True)
    tags = Column(JSON, nullable=True)
    depends_on_keys = Column(JSON, nullable=True)
    raw_payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TaskSplitDependency(Base):
    __tablename__ = "task_split_dependencies"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    plan_id = Column(String, nullable=False, index=True)
    predecessor_node_id = Column(String, nullable=True, index=True)
    successor_node_id = Column(String, nullable=True, index=True)
    predecessor_key = Column(String(64), nullable=False, index=True)
    successor_key = Column(String(64), nullable=False, index=True)
    dependency_type = Column(String(32), nullable=False, default="blocks")
    rationale = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
