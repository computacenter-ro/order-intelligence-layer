"""[2] Mock Elasticsearch — Log Collector contract + the ring-buffer cap.

The collector is deliberately dumb (CLAUDE.md [2]), but two of its properties are
load-bearing for the rest of the system and are pinned here:

* the ``?from=&to=`` half-open window and ascending sort — the AI-service poller
  depends on both (a poller window that silently dropped or misordered logs
  starves the journey assembler);
* the ``MAX_LOGS`` ring buffer — the store is unbounded by nature and the
  deployed simulation runs continuously, so eviction must be bounded, oldest
  first, and must never break the range query for logs still in the window.

The cap tests rebuild the store at a small capacity rather than pushing 200k
dicts through the real default: capacity is a parameter, so proving the eviction
*policy* at n=5 proves it at n=200000.
"""
from collections import deque

import pytest
from fastapi.testclient import TestClient

from pipeline.mock_es import app as mock_es


@pytest.fixture(autouse=True)
def _clean_store():
    """Each test starts with an empty store at the module's real capacity."""
    mock_es._STORE.clear()
    yield
    mock_es._STORE.clear()


@pytest.fixture
def client():
    return TestClient(mock_es.app)


def _log(log_id: str, timestamp: str, **fields) -> dict:
    return {"log_id": log_id, "timestamp": timestamp, **fields}


def _capped(monkeypatch, capacity: int) -> None:
    """Swap in a store with a small capacity (the policy is capacity-independent)."""
    monkeypatch.setattr(mock_es, "_STORE", deque(maxlen=capacity))
    monkeypatch.setattr(mock_es, "MAX_LOGS", capacity)


# --- Existing contract (must survive the deque change) ------------------------
def test_ingest_single_and_array(client):
    assert client.post("/logs", json=_log("a", "2026-07-14T08:00:00.000Z")).json() == {
        "ingested": 1
    }
    payload = [_log("b", "2026-07-14T08:00:01.000Z"), _log("c", "2026-07-14T08:00:02.000Z")]
    assert client.post("/logs", json=payload).json() == {"ingested": 2}
    assert len(client.get("/logs").json()) == 3


def test_ingest_rejects_missing_fields(client):
    assert client.post("/logs", json={"timestamp": "2026-07-14T08:00:00.000Z"}).status_code == 422
    assert client.post("/logs", json={"log_id": "x"}).status_code == 422


def test_a_rejected_batch_stores_nothing(client):
    """Validation covers the whole batch, so a bad line can't half-ingest it."""
    bad = [_log("ok", "2026-07-14T08:00:00.000Z"), {"log_id": "no-timestamp"}]
    assert client.post("/logs", json=bad).status_code == 422
    assert client.get("/logs").json() == []


def test_range_is_half_open_and_sorted_ascending(client):
    # Ingested out of timestamp order on purpose — the response must still sort.
    client.post(
        "/logs",
        json=[
            _log("t2", "2026-07-14T08:00:02.000Z"),
            _log("t0", "2026-07-14T08:00:00.000Z"),
            _log("t1", "2026-07-14T08:00:01.000Z"),
        ],
    )
    got = client.get(
        "/logs", params={"from": "2026-07-14T08:00:00.000Z", "to": "2026-07-14T08:00:02.000Z"}
    ).json()
    # from is inclusive, to is exclusive => t0, t1 but not t2.
    assert [log["log_id"] for log in got] == ["t0", "t1"]


def test_query_by_id_matches_any_id_family(client):
    client.post(
        "/logs",
        json=[
            _log("l1", "2026-07-14T08:00:00.000Z", eventId="evt-abc"),
            _log("l2", "2026-07-14T08:00:01.000Z", orderId="ORD-6001"),
            _log("l3", "2026-07-14T08:00:02.000Z", cartHeaderId="1840927365018240001"),
            _log("l4", "2026-07-14T08:00:03.000Z", eventId="evt-other"),
        ],
    )
    for value, expected in [
        ("evt-abc", "l1"),
        ("ORD-6001", "l2"),
        ("1840927365018240001", "l3"),
    ]:
        got = client.get("/logs", params={"id": value}).json()
        assert [log["log_id"] for log in got] == [expected]


# --- The ring-buffer cap -----------------------------------------------------
def test_store_never_exceeds_capacity(monkeypatch, client):
    _capped(monkeypatch, 5)
    for i in range(20):
        client.post("/logs", json=_log(f"l{i}", f"2026-07-14T08:00:{i:02d}.000Z"))
    assert len(mock_es._STORE) == 5


def test_eviction_drops_oldest_and_keeps_newest(monkeypatch, client):
    _capped(monkeypatch, 3)
    for i in range(5):
        client.post("/logs", json=_log(f"l{i}", f"2026-07-14T08:00:{i:02d}.000Z"))
    kept = [log["log_id"] for log in client.get("/logs").json()]
    assert kept == ["l2", "l3", "l4"]  # l0/l1 evicted


def test_a_single_batch_larger_than_capacity_keeps_the_tail(monkeypatch, client):
    """An over-capacity batch must not blow the cap — deque keeps the last N."""
    _capped(monkeypatch, 3)
    batch = [_log(f"l{i}", f"2026-07-14T08:00:{i:02d}.000Z") for i in range(10)]
    assert client.post("/logs", json=batch).json() == {"ingested": 10}
    assert len(mock_es._STORE) == 3
    assert [log["log_id"] for log in client.get("/logs").json()] == ["l7", "l8", "l9"]


def test_eviction_is_by_insertion_order_not_timestamp(monkeypatch, client):
    """Interleaved flows mean newest-arrived != latest-timestamp.

    A log that arrives LAST but carries an EARLIER timestamp is still the most
    recent arrival and must be retained — it may still sit inside the poller's
    window. Evicting by timestamp would drop it. This is the case that makes
    insertion-order eviction the correct policy, not merely the simpler one.
    """
    _capped(monkeypatch, 2)
    client.post("/logs", json=_log("early-arrival", "2026-07-14T08:00:09.000Z"))
    client.post("/logs", json=_log("mid", "2026-07-14T08:00:05.000Z"))
    # Arrives last, earliest timestamp (a slow concurrent flow catching up).
    client.post("/logs", json=_log("late-arrival", "2026-07-14T08:00:01.000Z"))

    kept = {log["log_id"] for log in client.get("/logs").json()}
    assert kept == {"mid", "late-arrival"}  # the FIRST-inserted log is the one dropped


def test_range_query_still_serves_logs_inside_the_window_after_eviction(monkeypatch, client):
    """The property the poller actually depends on, stated directly."""
    _capped(monkeypatch, 4)
    for i in range(10):  # 6 evictions
        client.post("/logs", json=_log(f"l{i}", f"2026-07-14T08:00:{i:02d}.000Z"))
    got = client.get(
        "/logs", params={"from": "2026-07-14T08:00:06.000Z", "to": "2026-07-14T08:00:10.000Z"}
    ).json()
    assert [log["log_id"] for log in got] == ["l6", "l7", "l8", "l9"]


def test_health_reports_occupancy_and_capacity(monkeypatch, client):
    _capped(monkeypatch, 5)
    client.post("/logs", json=_log("a", "2026-07-14T08:00:00.000Z"))
    assert client.get("/health").json() == {
        "status": "ok",
        "stored": 1,
        "capacity": 5,
        "oldest_timestamp": "2026-07-14T08:00:00.000Z",
    }


# --- retention floor (oldest_timestamp) --------------------------------------
# The poller uses this to tell "quiet period" from "my watermark predates
# everything I still hold" — the second case means logs were lost to eviction or
# to a restart of this in-memory store.
def test_oldest_timestamp_is_null_when_empty(client):
    assert client.get("/health").json()["oldest_timestamp"] is None


def test_oldest_timestamp_advances_as_logs_are_evicted(monkeypatch, client):
    _capped(monkeypatch, 3)
    for i in range(6):
        client.post("/logs", json=_log(f"l{i}", f"2026-07-14T08:00:{i:02d}.000Z"))
    # l0..l2 evicted; the floor is now l3's timestamp.
    assert client.get("/health").json()["oldest_timestamp"] == "2026-07-14T08:00:03.000Z"


def test_oldest_timestamp_is_the_minimum_not_the_first_inserted(monkeypatch, client):
    """Eviction is by insertion order, but this is a claim about TIME.

    Interleaved flows mean the left-most retained entry is not necessarily the
    earliest, so returning ``_STORE[0]`` would overstate the floor and make the
    poller skip past logs it actually still had.
    """
    _capped(monkeypatch, 3)
    client.post("/logs", json=_log("first-in", "2026-07-14T08:00:20.000Z"))
    client.post("/logs", json=_log("mid", "2026-07-14T08:00:30.000Z"))
    client.post("/logs", json=_log("late-arrival", "2026-07-14T08:00:05.000Z"))
    assert client.get("/health").json()["oldest_timestamp"] == "2026-07-14T08:00:05.000Z"


# --- unfiltered query limit --------------------------------------------------
def test_unfiltered_query_is_capped_by_default(monkeypatch, client):
    """A no-params read must not serialize the whole store (~90 MB at 200k)."""
    monkeypatch.setattr(mock_es, "DEFAULT_QUERY_LIMIT", 5)
    for i in range(20):
        client.post("/logs", json=_log(f"l{i}", f"2026-07-14T08:00:{i:02d}.000Z"))
    got = client.get("/logs").json()
    assert len(got) == 5
    # The NEWEST ones — the only reason to read without a window is recent activity.
    assert [log["log_id"] for log in got] == ["l15", "l16", "l17", "l18", "l19"]


def test_explicit_limit_overrides_the_default(monkeypatch, client):
    monkeypatch.setattr(mock_es, "DEFAULT_QUERY_LIMIT", 2)
    for i in range(10):
        client.post("/logs", json=_log(f"l{i}", f"2026-07-14T08:00:{i:02d}.000Z"))
    assert len(client.get("/logs", params={"limit": 7}).json()) == 7


def test_range_query_is_NOT_capped_by_the_default(monkeypatch, client):
    """The poller's correctness depends on getting its ENTIRE window.

    Silently truncating a windowed read would drop logs and starve journey
    assembly — exactly the failure the limit is meant to avoid causing.
    """
    monkeypatch.setattr(mock_es, "DEFAULT_QUERY_LIMIT", 3)
    for i in range(10):
        client.post("/logs", json=_log(f"l{i}", f"2026-07-14T08:00:{i:02d}.000Z"))
    got = client.get(
        "/logs", params={"from": "2026-07-14T08:00:00.000Z", "to": "2026-07-14T08:00:10.000Z"}
    ).json()
    assert len(got) == 10        # all of them, despite the low default


def test_id_query_is_NOT_capped_by_the_default(monkeypatch, client):
    monkeypatch.setattr(mock_es, "DEFAULT_QUERY_LIMIT", 2)
    for i in range(6):
        client.post(
            "/logs",
            json=_log(f"l{i}", f"2026-07-14T08:00:{i:02d}.000Z", eventId="evt-same"),
        )
    assert len(client.get("/logs", params={"id": "evt-same"}).json()) == 6


def test_limit_must_be_positive(client):
    assert client.get("/logs", params={"limit": 0}).status_code == 422


def test_max_logs_is_env_configurable(monkeypatch):
    """MOCK_ES_MAX_LOGS is read at import, so reload to observe an override."""
    import importlib

    monkeypatch.setenv("MOCK_ES_MAX_LOGS", "7")
    reloaded = importlib.reload(mock_es)
    try:
        assert reloaded.MAX_LOGS == 7
        assert reloaded._STORE.maxlen == 7
    finally:
        monkeypatch.delenv("MOCK_ES_MAX_LOGS", raising=False)
        importlib.reload(mock_es)  # restore the default for other tests
