"""drop alerts.confidence (router no longer produces a confidence score)

Revision ID: c9b3f07a51de
Revises: 04f7c3a56d53
Create Date: 2026-07-30 10:05:12.664301

The router LLM is no longer asked for a confidence score, so the column has no
producer: it would sit NULL on every new row while still appearing in the API
response and the dashboard's type. Removed end to end instead — the router now
returns ``{department, severity}`` only.

Severity and department are untouched; this migration is only about the score.

``downgrade`` re-adds the column as a nullable Float, which is exactly its old
definition. It cannot restore the VALUES (they are dropped with the column) —
downgrading yields the right shape with NULL everywhere, which is also what a
re-added column would hold for any row written while the score did not exist.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9b3f07a51de'
down_revision: Union[str, Sequence[str], None] = '04f7c3a56d53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("alerts", "confidence")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "alerts",
        sa.Column("confidence", sa.Float(), nullable=True),
    )
