"""DB-free checks that the ORM models have the columns backend/incidents.py
needs. No database — just inspects the mapped columns."""
from __future__ import annotations

from backend.db import Alert, Incident, Journey


def test_alert_has_embedding_and_incident_id_columns():
    columns = {c.name for c in Alert.__table__.columns}
    assert "embedding" in columns
    assert "incident_id" in columns


def test_journey_has_incident_id_column():
    columns = {c.name for c in Journey.__table__.columns}
    assert "incident_id" in columns


def test_incident_table_has_expected_columns():
    columns = {c.name for c in Incident.__table__.columns}
    assert columns == {
        "incident_id", "signature", "failure_subtype", "failing_service",
        "error_token", "title", "department", "status", "first_ts", "last_ts",
        "primary_alert_id", "alert_count", "journey_count",
    }
