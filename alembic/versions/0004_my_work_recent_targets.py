"""Add persisted My Work recent targets.

Revision ID: 0004_my_work_recent_targets
Revises: 0003_task_profile
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_my_work_recent_targets"
down_revision: Union[str, None] = "0003_task_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "my_work_recent_targets"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_key", sa.String(length=512), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=True),
        sa.Column("table_id", sa.String(length=128), nullable=True),
        sa.Column("view_id", sa.String(length=128), nullable=True),
        sa.Column("visited_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "target_key", name="uq_my_work_recent_user_target"),
    )
    op.create_index("ix_my_work_recent_targets_user_id", _TABLE, ["user_id"])
    op.create_index("ix_my_work_recent_targets_workspace_id", _TABLE, ["workspace_id"])
    op.create_index("ix_my_work_recent_targets_table_id", _TABLE, ["table_id"])
    op.create_index(
        "ix_my_work_recent_user_visited",
        _TABLE,
        ["user_id", "visited_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
