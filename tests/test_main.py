"""Tests for backend/main.py — lifespan wiring of API + WS + consumers.

No broker: ``run_consumers`` is monkeypatched, and the lifespan is driven
directly as an async context manager so we can assert (a) the consumers are
started wired to the shared WS hub's broadcast, and (b) the task is cancelled
cleanly on shutdown.
"""

import asyncio

import backend.main as main
from backend.ws import manager


def test_hub_is_the_shared_ws_manager():
    # The lifespan broadcasts through the very hub the /ws endpoint registers
    # clients into — one hub shared across API + WS + consumers.
    assert main.hub is manager


async def test_lifespan_starts_consumers_on_hub_and_cancels_cleanly(monkeypatch):
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
        assert captured["on_event"] == manager.broadcast  # wired to the hub

    # exiting the lifespan cancelled the task cleanly
    assert captured.get("cancelled") is True


async def test_lifespan_survives_consumer_failure(monkeypatch):
    # A broker outage (run_consumers raising) must not crash startup/shutdown.
    async def failing_run_consumers(on_event=None):
        raise RuntimeError("broker down")

    monkeypatch.setattr(main, "run_consumers", failing_run_consumers)

    async with main.lifespan(main.app):
        await asyncio.sleep(0.01)  # task fails, but the app stays up
    # no exception propagated out of the lifespan
