"""Persist workspace and member experience-mode preferences.

Revision ID: 0007_workspace_experience_mode
Revises: 0006_collaboration
Create Date: 2026-09-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_workspace_experience_mode"
down_revision: Union[str, None] = "0006_collaboration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    if "workspaces" in _tables() and "experience_mode" not in _columns("workspaces"):
        op.add_column(
            "workspaces",
            sa.Column(
                "experience_mode",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'simple'"),
            ),
        )

    if (
        "workspace_members" in _tables()
        and "experience_mode" not in _columns("workspace_members")
    ):
        op.add_column(
            "workspace_members",
            sa.Column("experience_mode", sa.String(length=16), nullable=True),
        )


def downgrade() -> None:
    if (
        "workspace_members" in _tables()
        and "experience_mode" in _columns("workspace_members")
    ):
        with op.batch_alter_table("workspace_members") as batch_op:
            batch_op.drop_column("experience_mode")

    if "workspaces" in _tables() and "experience_mode" in _columns("workspaces"):
        with op.batch_alter_table("workspaces") as batch_op:
            batch_op.drop_column("experience_mode")
