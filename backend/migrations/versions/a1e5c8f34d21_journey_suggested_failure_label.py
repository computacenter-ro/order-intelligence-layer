"""add journeys.suggested_failure_label column

Revision ID: a1e5c8f34d21
Revises: 4d8f21b6a9c3
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1e5c8f34d21'
down_revision: Union[str, Sequence[str], None] = '4d8f21b6a9c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "journeys",
        sa.Column("suggested_failure_label", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("journeys", "suggested_failure_label")
