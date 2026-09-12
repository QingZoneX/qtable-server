"""Open-source v0.1 schema baseline.

Revision ID: 0001_open_source_baseline
Revises:
Create Date: 2026-09-02

For an empty database, alembic/env.py materializes the current v0.1 metadata
before this revision is stamped. Existing installations are not recreated or
destructively altered by this baseline.
"""

from typing import Sequence, Union

revision: str = "0001_open_source_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
