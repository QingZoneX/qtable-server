"""Add optional table task/business semantic profile.

Revision ID: 0003_task_profile
Revises: 0002_oauth_session_id
Create Date: 2026-09-03

The migration is bootstrap-safe because QTable materializes current metadata on
completely empty databases before Alembic runs revisions.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_task_profile"
down_revision: Union[str, None] = "0002_oauth_session_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "table_task_profiles"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("table_id", sa.String(length=128), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("table_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
