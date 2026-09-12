"""Add durable automation rules, events and executions.

Revision ID: 0008_automation_engine
Revises: 0007_workspace_experience_mode
Create Date: 2026-09-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_automation_engine"
down_revision: Union[str, None] = "0007_workspace_experience_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "automation_rules" not in tables:
        op.create_table(
            "automation_rules",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("table_id", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=191), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("trigger", sa.JSON(), nullable=False),
            sa.Column("conditions", sa.JSON(), nullable=False),
            sa.Column("actions", sa.JSON(), nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
            sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("run_as_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_automation_rules_workspace_id", "automation_rules", ["workspace_id"])
        op.create_index("ix_automation_rules_table_id", "automation_rules", ["table_id"])
        op.create_index("ix_automation_rules_run_as_user_id", "automation_rules", ["run_as_user_id"])
        op.create_index("ix_automation_rules_next_run_at", "automation_rules", ["next_run_at"])
        op.create_index("ix_automation_rules_deleted_at", "automation_rules", ["deleted_at"])
        op.create_index("ix_automation_rules_table_enabled", "automation_rules", ["table_id", "enabled", "deleted_at"])
        op.create_index("ix_automation_rules_due", "automation_rules", ["enabled", "next_run_at", "deleted_at"])

    tables = _tables()
    if "automation_events" not in tables:
        op.create_table(
            "automation_events",
            sa.Column("id", sa.String(length=128), primary_key=True),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("table_id", sa.String(length=128), nullable=False),
            sa.Column("record_id", sa.String(length=128), nullable=True),
            sa.Column("source_change_item_id", sa.String(length=64), nullable=True),
            sa.Column("target_automation_id", sa.String(length=64), nullable=True),
            sa.Column("type", sa.String(length=32), nullable=False),
            sa.Column("before_data", sa.JSON(), nullable=True),
            sa.Column("after_data", sa.JSON(), nullable=True),
            sa.Column("changed_fields", sa.JSON(), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("source", sa.String(length=64), nullable=True),
            sa.Column("trace_id", sa.String(length=128), nullable=True),
            sa.Column("root_event_id", sa.String(length=128), nullable=False),
            sa.Column("parent_execution_id", sa.String(length=64), nullable=True),
            sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("source_change_item_id", name="uq_automation_events_source_change_item_id"),
        )
        for name, columns in (
            ("ix_automation_events_workspace_id", ["workspace_id"]),
            ("ix_automation_events_table_id", ["table_id"]),
            ("ix_automation_events_record_id", ["record_id"]),
            ("ix_automation_events_source_change_item_id", ["source_change_item_id"]),
            ("ix_automation_events_target_automation_id", ["target_automation_id"]),
            ("ix_automation_events_type", ["type"]),
            ("ix_automation_events_actor_user_id", ["actor_user_id"]),
            ("ix_automation_events_trace_id", ["trace_id"]),
            ("ix_automation_events_root_event_id", ["root_event_id"]),
            ("ix_automation_events_parent_execution_id", ["parent_execution_id"]),
            ("ix_automation_events_status", ["status"]),
            ("ix_automation_events_available_at", ["available_at"]),
            ("ix_automation_events_queue", ["status", "available_at", "created_at"]),
            ("ix_automation_events_table_type", ["table_id", "type", "created_at"]),
        ):
            op.create_index(name, "automation_events", columns)

    tables = _tables()
    if "automation_executions" not in tables:
        op.create_table(
            "automation_executions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("automation_id", sa.String(length=64), sa.ForeignKey("automation_rules.id", ondelete="CASCADE"), nullable=False),
            sa.Column("automation_version", sa.Integer(), nullable=False),
            sa.Column("trigger_event_id", sa.String(length=128), nullable=False),
            sa.Column("root_event_id", sa.String(length=128), nullable=False),
            sa.Column("parent_execution_id", sa.String(length=64), nullable=True),
            sa.Column("record_id", sa.String(length=128), nullable=True),
            sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("trace_id", sa.String(length=128), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("action_results", sa.JSON(), nullable=False),
            sa.Column("change_set_ids", sa.JSON(), nullable=False),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("automation_id", "automation_version", "trigger_event_id", name="uq_automation_execution_rule_version_event"),
            sa.UniqueConstraint("trace_id", name="uq_automation_executions_trace_id"),
        )
        for name, columns in (
            ("ix_automation_executions_automation_id", ["automation_id"]),
            ("ix_automation_executions_trigger_event_id", ["trigger_event_id"]),
            ("ix_automation_executions_root_event_id", ["root_event_id"]),
            ("ix_automation_executions_parent_execution_id", ["parent_execution_id"]),
            ("ix_automation_executions_record_id", ["record_id"]),
            ("ix_automation_executions_status", ["status"]),
            ("ix_automation_executions_trace_id", ["trace_id"]),
            ("ix_automation_executions_next_retry_at", ["next_retry_at"]),
            ("ix_automation_executions_rule_created", ["automation_id", "created_at"]),
            ("ix_automation_executions_retry", ["status", "next_retry_at"]),
        ):
            op.create_index(name, "automation_executions", columns)


def downgrade() -> None:
    tables = _tables()
    for table_name in ("automation_executions", "automation_events", "automation_rules"):
        if table_name in tables:
            op.drop_table(table_name)
