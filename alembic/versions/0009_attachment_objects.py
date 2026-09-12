"""Add durable attachment object registry.

Revision ID: 0009_attachment_objects
Revises: 0008_automation_engine
Create Date: 2026-09-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_attachment_objects"
down_revision: Union[str, None] = "0008_automation_engine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "attachment_objects" in _tables():
        return
    op.create_table(
        "attachment_objects",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("table_id", sa.String(length=128), nullable=False),
        sa.Column("record_id", sa.String(length=128), nullable=False),
        sa.Column("field_id", sa.String(length=128), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("cleanup_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cleanup_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_attachment_objects_object_key", "attachment_objects", ["object_key"], unique=True)
    op.create_index("ix_attachment_objects_table_id", "attachment_objects", ["table_id"])
    op.create_index("ix_attachment_objects_record_id", "attachment_objects", ["record_id"])
    op.create_index("ix_attachment_objects_field_id", "attachment_objects", ["field_id"])
    op.create_index("ix_attachment_objects_created_by_user_id", "attachment_objects", ["created_by_user_id"])
    op.create_index("ix_attachment_objects_status", "attachment_objects", ["status"])
    op.create_index("ix_attachment_objects_created_at", "attachment_objects", ["created_at"])
    op.create_index(
        "ix_attachment_objects_scope",
        "attachment_objects",
        ["table_id", "record_id", "field_id", "status"],
    )


def downgrade() -> None:
    if "attachment_objects" not in _tables():
        return
    op.drop_table("attachment_objects")
