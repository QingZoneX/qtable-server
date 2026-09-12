from sqlalchemy import Column, String, Text, DateTime, Integer, Boolean, JSON
from sqlalchemy.sql import func
from app.db.base import Base


class PMAgentWorkflow(Base):
    __tablename__ = "pm_agent_workflows"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    session_id = Column(String, nullable=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    workspace_id = Column(String, nullable=True)
    project_id = Column(String, nullable=True, index=True)
    team_id = Column(String, nullable=True)
    table_ids = Column(Text, nullable=True)
    user_message = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="initializing", index=True)

    phase_sequence = Column(JSON, nullable=True)
    current_phase = Column(String(64), nullable=True)
    completed_phases = Column(Integer, default=0)
    total_phases = Column(Integer, default=10)

    requirements_result = Column(JSON, nullable=True)
    modules_result = Column(JSON, nullable=True)
    task_tree_result = Column(JSON, nullable=True)
    workload_estimate_result = Column(JSON, nullable=True)
    member_assignment_result = Column(JSON, nullable=True)
    milestones_result = Column(JSON, nullable=True)
    gantt_result = Column(JSON, nullable=True)
    risk_analysis_result = Column(JSON, nullable=True)
    workflow_design_result = Column(JSON, nullable=True)
    report_result = Column(JSON, nullable=True)

    phase_results = Column(JSON, nullable=True)
    agent_memory_snapshot = Column(JSON, nullable=True)

    final_response = Column(Text, nullable=True)
    error_snapshot = Column(JSON, nullable=True)
    config_snapshot = Column(JSON, nullable=True)
    context_snapshot = Column(JSON, nullable=True)
    retry_policy = Column(JSON, nullable=True)
    metadata_snapshot = Column(JSON, nullable=True)

    langgraph_checkpoint_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class PMAgentPhaseResult(Base):
    __tablename__ = "pm_agent_phase_results"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    workflow_id = Column(String, nullable=False, index=True)
    phase = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(JSON, nullable=True)
    data = Column(JSON, nullable=True)
    token_usage = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
