"""add incidents table, embedding + incident_id columns

Revision ID: 4d8f21b6a9c3
Revises: d7c4b91e2a08
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '4d8f21b6a9c3'
down_revision: Union[str, Sequence[str], None] = 'd7c4b91e2a08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # incidents references alerts.alert_id (primary_alert_id) — alerts already
    # exists from the initial migration, so this ordering is fine.
    op.create_table(
        "incidents",
        sa.Column("incident_id", sa.String(), primary_key=True),
        sa.Column("signature", sa.String(), nullable=True),
        sa.Column("failure_subtype", sa.String(), nullable=True),
        sa.Column("failing_service", sa.String(), nullable=True),
        sa.Column("error_token", sa.String(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("first_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "primary_alert_id", sa.String(),
            sa.ForeignKey("alerts.alert_id"), nullable=True,
        ),
        sa.Column("alert_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("journey_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_incidents_signature", "incidents", ["signature"])

    op.add_column("alerts", sa.Column("embedding", postgresql.JSONB(), nullable=True))
    op.add_column(
        "alerts",
        sa.Column(
            "incident_id", sa.String(),
            sa.ForeignKey("incidents.incident_id"), nullable=True,
        ),
    )
    op.create_index("ix_alerts_incident_id", "alerts", ["incident_id"])

    op.add_column(
        "journeys",
        sa.Column(
            "incident_id", sa.String(),
            sa.ForeignKey("incidents.incident_id"), nullable=True,
        ),
    )
    op.create_index("ix_journeys_incident_id", "journeys", ["incident_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_journeys_incident_id", table_name="journeys")
    op.drop_column("journeys", "incident_id")
    op.drop_index("ix_alerts_incident_id", table_name="alerts")
    op.drop_column("alerts", "incident_id")
    op.drop_column("alerts", "embedding")
    op.drop_index("ix_incidents_signature", table_name="incidents")
    op.drop_table("incidents")
