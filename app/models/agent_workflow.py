from sqlalchemy import Column, String, Text, DateTime, Integer, Boolean, JSON
from sqlalchemy.sql import func
from app.db.base import Base


class AgentWorkflow(Base):
    __tablename__ = "agent_workflows"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    session_id = Column(String, nullable=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    workspace_id = Column(String, nullable=True)
    table_ids = Column(Text, nullable=True)
    user_message = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="initializing", index=True)
    plan_snapshot = Column(JSON, nullable=True)
    current_step_index = Column(Integer, default=-1)
    tool_calls_snapshot = Column(JSON, nullable=True)
    observations_snapshot = Column(JSON, nullable=True)
    pending_approvals_snapshot = Column(JSON, nullable=True)
    approval_history_snapshot = Column(JSON, nullable=True)
    final_response = Column(Text, nullable=True)
    error_snapshot = Column(JSON, nullable=True)
    config_snapshot = Column(JSON, nullable=True)
    context_snapshot = Column(JSON, nullable=True)
    retry_policy = Column(JSON, nullable=True)
    metadata_snapshot = Column(JSON, nullable=True)
    langgraph_checkpoint_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class AgentWorkflowStep(Base):
    __tablename__ = "agent_workflow_steps"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    workflow_id = Column(String, nullable=False, index=True)
    step_index = Column(Integer, nullable=False)
    description = Column(Text, nullable=True)
    skill_name = Column(String, nullable=True)
    arguments = Column(JSON, nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    depends_on = Column(JSON, nullable=True)
    max_retries = Column(Integer, default=3)
    retry_count = Column(Integer, default=0)
    require_approval = Column(Boolean, default=False)
    result = Column(JSON, nullable=True)
    error = Column(JSON, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AgentWorkflowHumanApproval(Base):
    __tablename__ = "agent_workflow_human_approvals"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    workflow_id = Column(String, nullable=False, index=True)
    call_id = Column(String, nullable=False)
    skill_name = Column(String, nullable=False)
    function_name = Column(String, nullable=False)
    arguments = Column(JSON, nullable=True)
    reason = Column(Text, nullable=True)
    preview = Column(JSON, nullable=True)
    status = Column(String(16), nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)
