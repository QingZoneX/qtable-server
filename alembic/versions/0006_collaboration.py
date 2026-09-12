"""Add record comments, structured mentions, and user notifications.

Revision ID: 0006_collaboration
Revises: 0005_board_card_order
Create Date: 2026-09-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_collaboration"
down_revision: Union[str, None] = "0005_board_card_order"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()

    if "record_comments" not in existing:
        op.create_table(
            "record_comments",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("table_id", sa.String(length=128), nullable=False),
            sa.Column("record_id", sa.String(length=128), nullable=False),
            sa.Column("author_id", sa.Integer(), nullable=True),
            sa.Column("parent_comment_id", sa.String(length=64), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("body_format", sa.String(length=16), server_default="markdown", nullable=False),
            sa.Column("client_mutation_id", sa.String(length=128), nullable=True),
            sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "author_id",
                "client_mutation_id",
                name="uq_record_comment_author_client_mutation",
            ),
        )
        op.create_index("ix_record_comments_workspace_id", "record_comments", ["workspace_id"])
        op.create_index("ix_record_comments_table_id", "record_comments", ["table_id"])
        op.create_index("ix_record_comments_record_id", "record_comments", ["record_id"])
        op.create_index("ix_record_comments_author_id", "record_comments", ["author_id"])
        op.create_index("ix_record_comments_parent_comment_id", "record_comments", ["parent_comment_id"])
        op.create_index("ix_record_comments_created_at", "record_comments", ["created_at"])
        op.create_index("ix_record_comments_deleted_at", "record_comments", ["deleted_at"])
        op.create_index(
            "ix_record_comments_record_created",
            "record_comments",
            ["table_id", "record_id", "created_at", "id"],
        )

    existing = _tables()
    if "record_comment_mentions" not in existing:
        op.create_table(
            "record_comment_mentions",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("comment_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["comment_id"], ["record_comments.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("comment_id", "user_id", name="uq_record_comment_mention"),
        )
        op.create_index("ix_record_comment_mentions_comment_id", "record_comment_mentions", ["comment_id"])
        op.create_index("ix_record_comment_mentions_user_id", "record_comment_mentions", ["user_id"])
        op.create_index(
            "ix_record_comment_mentions_user",
            "record_comment_mentions",
            ["user_id", "comment_id"],
        )

    existing = _tables()
    if "user_notifications" not in existing:
        op.create_table(
            "user_notifications",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("recipient_user_id", sa.Integer(), nullable=False),
            sa.Column("type", sa.String(length=32), nullable=False),
            sa.Column("actor_id", sa.Integer(), nullable=True),
            sa.Column("workspace_id", sa.String(length=128), nullable=True),
            sa.Column("table_id", sa.String(length=128), nullable=True),
            sa.Column("record_id", sa.String(length=128), nullable=True),
            sa.Column("comment_id", sa.String(length=64), nullable=True),
            sa.Column("event_id", sa.String(length=128), nullable=False),
            sa.Column("dedupe_key", sa.String(length=191), nullable=False),
            sa.Column("payload", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "recipient_user_id",
                "dedupe_key",
                name="uq_user_notification_recipient_dedupe",
            ),
        )
        for column in (
            "recipient_user_id",
            "type",
            "actor_id",
            "workspace_id",
            "table_id",
            "record_id",
            "comment_id",
            "event_id",
            "created_at",
            "read_at",
        ):
            op.create_index(f"ix_user_notifications_{column}", "user_notifications", [column])
        op.create_index(
            "ix_user_notifications_recipient_created",
            "user_notifications",
            ["recipient_user_id", "created_at", "id"],
        )
        op.create_index(
            "ix_user_notifications_recipient_unread",
            "user_notifications",
            ["recipient_user_id", "read_at", "created_at"],
        )


def downgrade() -> None:
    existing = _tables()
    if "user_notifications" in existing:
        op.drop_table("user_notifications")
    existing = _tables()
    if "record_comment_mentions" in existing:
        op.drop_table("record_comment_mentions")
    existing = _tables()
    if "record_comments" in existing:
        op.drop_table("record_comments")
