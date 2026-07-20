"""Tests for backend/main.py — lifespan wiring of API + WS + consumers + Teams.

No broker: ``run_consumers`` is monkeypatched, and the lifespan is driven
directly as an async context manager. We assert (a) the consumers are started
wired to the fan-out sink, (b) the fan-out delivers to both the WS hub and Teams
and isolates a failing sink, and (c) the task is cancelled cleanly on shutdown.
"""

import asyncio

import backend.main as main
from backend.ws import manager


def test_hub_is_the_shared_ws_manager():
    # The lifespan broadcasts through the very hub the /ws endpoint registers
    # clients into — one hub shared across API + WS + consumers.
    assert main.hub is manager


async def test_lifespan_starts_consumers_on_fanout_and_cancels_cleanly(monkeypatch):
    captured: dict = {}

    async def fake_run_consumers(on_event=None):
        captured["on_event"] = on_event
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            captured["cancelled"] = True
            raise

    monkeypatch.setattr(main, "run_consumers", fake_run_consumers)

    async with main.lifespan(main.app):
        await asyncio.sleep(0.01)  # let the background task start
        assert captured["on_event"] is main._fan_out  # wired to the fan-out sink

    # exiting the lifespan cancelled the task cleanly
    assert captured.get("cancelled") is True


async def test_fan_out_delivers_to_both_sinks(monkeypatch):
    ws_seen, teams_seen = [], []

    async def fake_broadcast(ev):
        ws_seen.append(ev)

    async def fake_notify(ev):
        teams_seen.append(ev)

    monkeypatch.setattr(main.hub, "broadcast", fake_broadcast)
    monkeypatch.setattr(main.teams, "notify", fake_notify)

    event = {"type": "alert.new", "data": {"alert_id": "a1"}}
    await main._fan_out(event)

    assert ws_seen == [event]
    assert teams_seen == [event]


async def test_fan_out_isolates_a_failing_sink(monkeypatch, capsys):
    teams_seen = []

    async def boom(ev):
        raise RuntimeError("hub down")

    async def fake_notify(ev):
        teams_seen.append(ev)

    # the WS sink fails, the Teams sink must still receive the event
    monkeypatch.setattr(main.hub, "broadcast", boom)
    monkeypatch.setattr(main.teams, "notify", fake_notify)

    event = {"type": "journey.completed", "data": {"journey_id": "J1"}}
    await main._fan_out(event)  # must not raise

    assert teams_seen == [event]
    assert "ws sink failed" in capsys.readouterr().out


async def test_lifespan_survives_consumer_failure(monkeypatch):
    # A broker outage (run_consumers raising) must not crash startup/shutdown.
    async def failing_run_consumers(on_event=None):
        raise RuntimeError("broker down")

    monkeypatch.setattr(main, "run_consumers", failing_run_consumers)

    async with main.lifespan(main.app):
        await asyncio.sleep(0.01)  # task fails, but the app stays up
    # no exception propagated out of the lifespan
