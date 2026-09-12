from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutomationRule(Base):
    """Persisted user-configurable automation definition.

    Rules are soft-deleted so historical executions keep a stable audit target.
    `version` changes whenever executable semantics change and participates in
    execution idempotency together with the triggering event id.
    """

    __tablename__ = "automation_rules"
    __table_args__ = (
        Index("ix_automation_rules_table_enabled", "table_id", "enabled", "deleted_at"),
        Index("ix_automation_rules_due", "enabled", "next_run_at", "deleted_at"),
    )

    id = Column(String(64), primary_key=True)
    workspace_id = Column(String(128), nullable=False, index=True)
    table_id = Column(String(128), nullable=False, index=True)
    name = Column(String(191), nullable=False)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=False)
    trigger = Column(JSON, nullable=False, default=dict)
    conditions = Column(JSON, nullable=False, default=dict)
    actions = Column(JSON, nullable=False, default=list)
    timezone = Column(String(64), nullable=False, default="UTC")
    max_retries = Column(Integer, nullable=False, default=3)
    version = Column(Integer, nullable=False, default=1)
    run_as_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    next_run_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)


class AutomationEvent(Base):
    """Durable record/schedule event queue consumed by the automation worker."""

    __tablename__ = "automation_events"
    __table_args__ = (
        Index("ix_automation_events_queue", "status", "available_at", "created_at"),
        Index("ix_automation_events_table_type", "table_id", "type", "created_at"),
    )

    id = Column(String(128), primary_key=True)
    workspace_id = Column(String(128), nullable=False, index=True)
    table_id = Column(String(128), nullable=False, index=True)
    record_id = Column(String(128), nullable=True, index=True)
    # Exactly one durable record event is materialized for each ChangeItem.
    # Synthetic scheduled/due/manual events leave this null.
    source_change_item_id = Column(String(64), nullable=True, unique=True, index=True)
    # Synthetic scheduled/due/manual events target exactly one rule. Record
    # events leave this null and are matched against every enabled rule on the table.
    target_automation_id = Column(String(64), nullable=True, index=True)
    type = Column(String(32), nullable=False, index=True)
    before_data = Column(JSON, nullable=True)
    after_data = Column(JSON, nullable=True)
    changed_fields = Column(JSON, nullable=False, default=list)
    actor_user_id = Column(Integer, nullable=True, index=True)
    source = Column(String(64), nullable=True)
    trace_id = Column(String(128), nullable=True, index=True)
    root_event_id = Column(String(128), nullable=False, index=True)
    parent_execution_id = Column(String(64), nullable=True, index=True)
    depth = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="queued", index=True)
    available_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    processed_at = Column(DateTime(timezone=True), nullable=True)


class AutomationExecution(Base):
    """One idempotent execution for one rule version and trigger event."""

    __tablename__ = "automation_executions"
    __table_args__ = (
        UniqueConstraint(
            "automation_id",
            "automation_version",
            "trigger_event_id",
            name="uq_automation_execution_rule_version_event",
        ),
        Index("ix_automation_executions_rule_created", "automation_id", "created_at"),
        Index("ix_automation_executions_retry", "status", "next_retry_at"),
    )

    id = Column(String(64), primary_key=True)
    automation_id = Column(
        String(64),
        ForeignKey("automation_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    automation_version = Column(Integer, nullable=False)
    trigger_event_id = Column(String(128), nullable=False, index=True)
    root_event_id = Column(String(128), nullable=False, index=True)
    parent_execution_id = Column(String(64), nullable=True, index=True)
    record_id = Column(String(128), nullable=True, index=True)
    depth = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="queued", index=True)
    trace_id = Column(String(128), nullable=False, unique=True, index=True)
    attempt = Column(Integer, nullable=False, default=0)
    action_results = Column(JSON, nullable=False, default=list)
    change_set_ids = Column(JSON, nullable=False, default=list)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
