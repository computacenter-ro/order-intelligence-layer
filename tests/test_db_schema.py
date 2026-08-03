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


def test_journey_has_suggested_failure_label_column():
    columns = {c.name for c in Journey.__table__.columns}
    assert "suggested_failure_label" in columns


def test_incident_table_has_expected_columns():
    columns = {c.name for c in Incident.__table__.columns}
    assert columns == {
        "incident_id", "signature", "failure_subtype", "failing_service",
        "error_token", "title", "department", "status", "first_ts", "last_ts",
        "primary_alert_id", "alert_count", "journey_count",
    }


# =============================================================================
# TLS to a managed Postgres (DB_SSL / sslmode normalization)
#
# These are pure URL-shaping checks — no database is touched.
#
# The trap being guarded: SQLAlchemy's asyncpg dialect forwards unknown URL query
# params straight to ``asyncpg.connect()``, and asyncpg has NO ``sslmode``
# parameter (only ``ssl``). psycopg2 does accept ``sslmode``, so the connection
# string Azure hands you is easy to copy across — and it raises
# ``TypeError: connect() got an unexpected keyword argument 'sslmode'`` on the
# first connection, well after startup looked healthy.
# =============================================================================
from backend.db import _engine_kwargs

_LOCAL = "postgresql+asyncpg://oil:oil@localhost:5432/oil"
_AZURE = "postgresql+asyncpg://u:pw@srv.postgres.database.azure.com:5432/oil"


def test_local_url_is_untouched_without_the_flag():
    """Local docker-compose behavior must be identical to before."""
    url, kwargs = _engine_kwargs(_LOCAL, "")
    assert dict(url.query) == {}
    assert kwargs == {"future": True}


def test_db_ssl_require_adds_ssl_for_asyncpg():
    url, _ = _engine_kwargs(_AZURE, "require")
    assert url.query["ssl"] == "require"


def test_db_ssl_off_values_add_nothing():
    for value in ("", "disable", "prefer", "allow", "false", "0"):
        url, _ = _engine_kwargs(_AZURE, value)
        assert "ssl" not in url.query, value


def test_sslmode_in_the_url_is_translated_to_ssl_for_asyncpg():
    """The Azure-copied form must work rather than TypeError on first connect."""
    url, _ = _engine_kwargs(f"{_AZURE}?sslmode=require", "")
    assert url.query["ssl"] == "require"
    assert "sslmode" not in url.query


def test_verify_modes_map_to_require():
    # libpq's verify-ca/verify-full have no direct asyncpg string form.
    for mode in ("verify-ca", "verify-full"):
        url, _ = _engine_kwargs(f"{_AZURE}?sslmode={mode}", "")
        assert url.query["ssl"] == "require", mode


def test_explicit_ssl_in_the_url_wins_over_the_flag():
    url, _ = _engine_kwargs(f"{_AZURE}?ssl=verify-full", "require")
    assert url.query["ssl"] == "verify-full"


def test_psycopg2_keeps_sslmode_untouched():
    """Alembic uses the sync driver, which handles ``sslmode`` natively — the
    translation must not corrupt it."""
    url, _ = _engine_kwargs(
        "postgresql+psycopg2://u:pw@srv:5432/oil?sslmode=require", "require"
    )
    assert url.query["sslmode"] == "require"
    assert "ssl" not in url.query


def test_password_is_preserved_not_masked():
    """``str(URL)`` renders the password as ``***``; returning a stringified URL
    would hand the engine a literal ``***`` and every connection would fail
    authentication. The URL object must be returned instead."""
    url, _ = _engine_kwargs(f"{_AZURE}?sslmode=require", "")
    assert url.password == "pw"


def test_asyncpg_never_receives_an_sslmode_kwarg():
    """The end-to-end guarantee, asserted against the real dialect: whatever the
    input form, the driver is called with ``ssl`` and never ``sslmode``."""
    from sqlalchemy.dialects.postgresql import asyncpg as pg_asyncpg

    for url_str, flag in (
        (f"{_AZURE}?sslmode=require", ""),
        (_AZURE, "require"),
        (f"{_AZURE}?ssl=require", ""),
    ):
        url, _ = _engine_kwargs(url_str, flag)
        _args, params = pg_asyncpg.dialect().create_connect_args(url)
        assert "sslmode" not in params, url_str
        assert params.get("ssl") == "require", url_str
