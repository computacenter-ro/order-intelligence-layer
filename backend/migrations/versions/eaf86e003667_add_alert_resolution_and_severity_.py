"""add alert resolution and severity columns

Revision ID: eaf86e003667
Revises: 096af60a6533
Create Date: 2026-07-23 11:05:07.975542

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eaf86e003667'
down_revision: Union[str, Sequence[str], None] = 'c1a2f3d4e5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "alerts",
        sa.Column("is_resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "alerts", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("alerts", "resolved_at")
    op.drop_column("alerts", "is_resolved")
