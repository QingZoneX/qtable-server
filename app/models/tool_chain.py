from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class ToolChainRun(Base):
    __tablename__ = "tool_chain_runs"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=True, index=True)
    session_id = Column(String, nullable=True, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    project_id = Column(String, nullable=True, index=True)
    view_id = Column(String, nullable=True, index=True)
    task_id = Column(String, nullable=True, index=True)
    team_id = Column(String, nullable=True, index=True)
    organization_id = Column(String, nullable=True, index=True)
    workflow_id = Column(String, nullable=True, index=True)
    agent_id = Column(String, nullable=True, index=True)
    source_message = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="queued", index=True)
    current_step_id = Column(String, nullable=True, index=True)
    confirmed = Column(Boolean, nullable=False, default=False)
    dry_run = Column(Boolean, nullable=False, default=False)
    provider = Column(String(64), nullable=True)
    model = Column(String(120), nullable=True)
    trace_id = Column(String(64), nullable=False, index=True)
    request_payload = Column(JSON, nullable=False)
    plan_json = Column(JSON, nullable=True)
    context_json = Column(JSON, nullable=False)
    memory_json = Column(JSON, nullable=False, default=dict)
    result_json = Column(JSON, nullable=False, default=dict)
    pending_confirmation = Column(JSON, nullable=True)
    metrics_json = Column(JSON, nullable=False, default=dict)
    error_json = Column(JSON, nullable=True)
    summary = Column(Text, nullable=False, default="")
    created_by = Column(String(64), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ToolChainStepRun(Base):
    __tablename__ = "tool_chain_step_runs"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    step_id = Column(String(120), nullable=False, index=True)
    skill_name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=False, default="")
    order_index = Column(Integer, nullable=False, default=0)
    depends_on = Column(JSON, nullable=False, default=list)
    status = Column(String(32), nullable=False, default="pending", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    input_payload = Column(JSON, nullable=False, default=dict)
    output_payload = Column(JSON, nullable=True)
    error_json = Column(JSON, nullable=True)
    retry_policy = Column(JSON, nullable=False, default=dict)
    rollback_payload = Column(JSON, nullable=False, default=dict)
    context_snapshot = Column(JSON, nullable=False, default=dict)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ToolChainEventLog(Base):
    __tablename__ = "tool_chain_event_logs"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    trace_id = Column(String(64), nullable=False, index=True)
    step_id = Column(String(120), nullable=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
