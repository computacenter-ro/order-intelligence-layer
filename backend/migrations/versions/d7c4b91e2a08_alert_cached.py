"""add alerts.cached (semantic-cache provenance)

Revision ID: d7c4b91e2a08
Revises: eaf86e003667
Create Date: 2026-07-26 10:12:44.318920

``ProcessedAlert.cached`` (set by the AI service's semantic cache) was being
dropped at the DB boundary, so a reused answer was indistinguishable from a
freshly computed one. NOT NULL + server_default false mirrors the Pydantic
field (``cached: bool = False``) and backfills existing rows correctly: every
alert written before this migration predates the flag being persisted, so it
genuinely was not a cache hit.

``cached`` is a modifier ON ``source="ai"``, never an alternative to it — a hit
is still an AI answer, reused. Teams routing keys off ``source`` and is
unaffected.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7c4b91e2a08'
down_revision: Union[str, Sequence[str], None] = 'eaf86e003667'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "alerts",
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("alerts", "cached")
