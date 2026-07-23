"""add alerts.severity (per-log LLM severity rating)

Revision ID: c1a2f3d4e5b6
Revises: 096af60a6533
Create Date: 2026-07-23 09:20:00.000000

Adds the nullable ``severity`` column on ``alerts``: one of
critical/high/medium/low from the router LLM, NULL for source="fallback"
(and for rows written before this migration). Nullable so no backfill is
needed and the at-least-once consumers keep working unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c1a2f3d4e5b6'
down_revision: Union[str, Sequence[str], None] = '096af60a6533'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('alerts', sa.Column('severity', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('alerts', 'severity')
