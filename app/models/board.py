from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Integer, Numeric, String, event, delete
from sqlalchemy.sql import func

from app.db.base import Base
from app.models.smart_table import TableRecord, TableView


class BoardCardOrder(Base):
    """Per-view manual ordering metadata for a table record.

    Business state (status / assignee / other lane fields) remains in
    ``TableRecord.data``.  This table stores only presentation order so Kanban
    never becomes a second task database.
    """

    __tablename__ = "board_card_orders"

    table_id = Column(String(128), primary_key=True)
    view_id = Column(String(128), primary_key=True)
    record_id = Column(String(128), primary_key=True)
    rank = Column(Numeric(50, 20), nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["view_id", "table_id"],
            ["table_views.id", "table_views.table_id"],
            ondelete="CASCADE",
            name="fk_board_card_order_view",
        ),
        ForeignKeyConstraint(
            ["record_id", "table_id"],
            ["table_records.id", "table_records.table_id"],
            ondelete="CASCADE",
            name="fk_board_card_order_record",
        ),
    )


# SQLite does not always enforce foreign keys in application/test connections.
# The normal ORM delete paths therefore perform the same cleanup explicitly.
# PostgreSQL still benefits from the database cascades for bulk/hard deletes.
@event.listens_for(TableRecord, "after_delete")
def _cleanup_record_board_order(_mapper, connection, target: TableRecord) -> None:
    connection.execute(
        delete(BoardCardOrder).where(
            BoardCardOrder.table_id == target.table_id,
            BoardCardOrder.record_id == target.id,
        )
    )


@event.listens_for(TableView, "after_delete")
def _cleanup_view_board_order(_mapper, connection, target: TableView) -> None:
    connection.execute(
        delete(BoardCardOrder).where(
            BoardCardOrder.table_id == target.table_id,
            BoardCardOrder.view_id == target.id,
        )
    )
