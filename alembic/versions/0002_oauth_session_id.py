"""Add stable OAuth session identifiers.

Revision ID: 0002_oauth_session_id
Revises: 0001_open_source_baseline
Create Date: 2026-09-03

QTable's v0.1 Alembic bootstrap materializes current metadata for a completely
empty database before running revisions. The guards below therefore make this
revision safe for both:
- existing installations that still need the new column/index; and
- fresh databases where current metadata already contains them.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_oauth_session_id"
down_revision: Union[str, None] = "0001_open_source_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "oauth_refresh_tokens"
_COLUMN = "session_id"
_INDEX = "ix_oauth_refresh_tokens_session_id"


def _column_names(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}


def _index_names(bind) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(bind).get_indexes(_TABLE)
        if index.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if _COLUMN not in _column_names(bind):
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.add_column(sa.Column(_COLUMN, sa.String(length=64), nullable=True))

    # Re-inspect after the possible batch-table recreation before creating the
    # index. This also avoids duplicating the metadata-created index on fresh DBs.
    if _INDEX not in _index_names(bind):
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_index(_INDEX, [_COLUMN], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if _INDEX in _index_names(bind):
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_index(_INDEX)
    if _COLUMN in _column_names(bind):
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
