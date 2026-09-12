"""Add per-view Kanban card ordering.

Revision ID: 0005_board_card_order
Revises: 0004_my_work_recent_targets
Create Date: 2026-09-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_board_card_order"
down_revision: Union[str, None] = "0004_my_work_recent_targets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "board_card_orders"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("table_id", sa.String(length=128), nullable=False),
        sa.Column("view_id", sa.String(length=128), nullable=False),
        sa.Column("record_id", sa.String(length=128), nullable=False),
        sa.Column("rank", sa.Numeric(precision=50, scale=20), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("table_id", "view_id", "record_id"),
        sa.ForeignKeyConstraint(
            ["view_id", "table_id"],
            ["table_views.id", "table_views.table_id"],
            ondelete="CASCADE",
            name="fk_board_card_order_view",
        ),
        sa.ForeignKeyConstraint(
            ["record_id", "table_id"],
            ["table_records.id", "table_records.table_id"],
            ondelete="CASCADE",
            name="fk_board_card_order_record",
        ),
    )
    op.create_index(
        "ix_board_card_orders_view_rank",
        _TABLE,
        ["table_id", "view_id", "rank", "record_id"],
    )
    op.create_index(
        "ix_board_card_orders_record",
        _TABLE,
        ["table_id", "record_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
