"""add chat_feedback table (thumbs up/down on assistant answers)

Revision ID: f3a1c7d24e80
Revises: 4d8f21b6a9c3
Create Date: 2026-07-28 09:14:32.118904

Feedback is USER DATA, so unlike the retrieval index (in-memory + Redis,
rebuildable by ``backfill_rag``) it must be durable — hence Postgres.

One row per ANSWER, not per cited record. A vote rates the answer the agent read;
which of its sources deserve credit is a derivation (rank-weighted, see
``backend/feedback.py``), not a fact to store. Storing per-record would bake one
attribution rule into the data and make it impossible to change later.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f3a1c7d24e80'
down_revision: Union[str, Sequence[str], None] = '4d8f21b6a9c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "chat_feedback",
        # The answer being rated. Minted by /chat so the dashboard can vote on a
        # specific reply; PK so re-voting REPLACES rather than stacks.
        sa.Column("answer_id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # +1 like / -1 dislike. The CHECK keeps a stray 0 or 7 out of the ranking
        # math, where it would silently skew every boost.
        sa.Column("vote", sa.SmallInteger(), nullable=False),
        # What was asked, and what was cited IN RANK ORDER — position is the
        # signal, since attribution weights by citation rank.
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("record_ids", postgresql.JSONB(), nullable=False),
        # Context for later analysis, not used by the ranking math:
        #   answer_mode     — "ai" vs "retrieval-only" (was an LLM even involved?)
        #   scoped_kind/_id — the record the question was scoped to, if any
        sa.Column("answer_mode", sa.String(), nullable=True),
        sa.Column("scoped_kind", sa.String(), nullable=True),
        sa.Column("scoped_id", sa.String(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.CheckConstraint("vote IN (-1, 1)", name="ck_chat_feedback_vote"),
    )
    # Boosts are computed by scanning recent rows, so the sweep is by time.
    op.create_index("ix_chat_feedback_created_at", "chat_feedback", ["created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_chat_feedback_created_at", table_name="chat_feedback")
    op.drop_table("chat_feedback")
