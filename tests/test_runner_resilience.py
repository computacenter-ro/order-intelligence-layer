"""Startup-resilience tests for the emitter tier (pipeline/services/).

These cover the docker-compose boot race that used to leave ``mock-services``
running with NO AMQP consumers attached — a container that looks healthy to
Docker while draining no queues, which no restart policy can recover:

  * ``aio_pika.connect_robust`` only re-establishes a connection that was already
    open; it does NOT retry the *initial* connect. A broker that is not yet
    accepting AMQP connections therefore raised and killed the service task.
    ``runner._connect`` now retries with backoff (mirroring
    ``backend/main.py::_run_consumers_guarded``).
  * ``run_all`` gathered the per-service tasks WITHOUT ``return_exceptions=True``,
    so the first task to raise abandoned the other nine mid-flight. It now awaits
    them all, names every death, and exits non-zero so the restart policy has a
    signal.

Everything is faked — no broker, no network.
"""
from __future__ import annotations

import asyncio

import pytest

from pipeline.services import run_all, runner

pytestmark = pytest.mark.asyncio


# --- runner._connect: retries the INITIAL connect -----------------------------
async def test_connect_retries_until_the_broker_answers(monkeypatch):
    """A broker that is down at startup must be waited for, not fatal."""
    attempts = {"n": 0}
    sentinel = object()

    async def flaky(_url):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("[Errno 111] Connect call failed")
        return sentinel

    monkeypatch.setattr(runner.aio_pika, "connect_robust", flaky)
    monkeypatch.setattr(runner, "CONNECT_RETRY_DELAY", 0.0)
    monkeypatch.setattr(runner, "CONNECT_RETRY_MAX_DELAY", 0.0)

    conn = await runner._connect("inbound")
    assert conn is sentinel
    assert attempts["n"] == 3  # failed twice, succeeded on the third


async def test_connect_backoff_grows_and_is_capped(monkeypatch):
    """Backoff doubles up to CONNECT_RETRY_MAX_DELAY so a long outage can't spin."""
    slept: list[float] = []
    attempts = {"n": 0}

    async def always_fail(_url):
        attempts["n"] += 1
        if attempts["n"] > 5:
            return object()
        raise ConnectionError("refused")

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(runner.aio_pika, "connect_robust", always_fail)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(runner, "CONNECT_RETRY_DELAY", 1.0)
    monkeypatch.setattr(runner, "CONNECT_RETRY_MAX_DELAY", 4.0)

    await runner._connect("inbound")
    assert slept == [1.0, 2.0, 4.0, 4.0, 4.0]  # doubles, then pinned at the cap


@pytest.mark.parametrize("exc_type", runner.PERMANENT_CONNECT_ERRORS)
async def test_connect_does_not_retry_authentication_failures(monkeypatch, exc_type):
    """An auth rejection is PERMANENT — retrying it would hide a config mistake.

    The broker answered and said no, so waiting cannot help. Looping here would
    swap one silent failure for another: a mistyped credential leaves a container
    Docker reports as healthy while it consumes nothing, forever.
    """
    attempts = {"n": 0}

    async def refused(_url):
        attempts["n"] += 1
        raise exc_type("ACCESS_REFUSED - Login was refused")

    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(runner.aio_pika, "connect_robust", refused)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)

    with pytest.raises(exc_type):
        await runner._connect("inbound")

    assert attempts["n"] == 1, "auth failure must not be retried"
    assert slept == [], "must not sleep before re-raising a permanent error"


async def test_probable_auth_error_is_not_a_subclass_of_auth_error():
    """Why PERMANENT_CONNECT_ERRORS lists BOTH classes.

    Bad credentials actually raise ``ProbableAuthenticationError``, which is NOT a
    subclass of ``AuthenticationError`` — catching either alone silently misses the
    other, and the retry loop would swallow it.
    """
    assert not issubclass(
        runner.aio_pika.exceptions.ProbableAuthenticationError,
        runner.aio_pika.exceptions.AuthenticationError,
    )
    assert runner.aio_pika.exceptions.ProbableAuthenticationError in runner.PERMANENT_CONNECT_ERRORS
    assert runner.aio_pika.exceptions.AuthenticationError in runner.PERMANENT_CONNECT_ERRORS


async def test_connect_still_retries_a_refused_connection(monkeypatch):
    """The transient case must be unaffected by the auth carve-out."""
    attempts = {"n": 0}

    async def refused_then_ok(_url):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("[Errno 111] Connect call failed")
        return object()

    monkeypatch.setattr(runner.aio_pika, "connect_robust", refused_then_ok)
    monkeypatch.setattr(runner, "CONNECT_RETRY_DELAY", 0.0)
    monkeypatch.setattr(runner, "CONNECT_RETRY_MAX_DELAY", 0.0)

    assert await runner._connect("inbound") is not None
    assert attempts["n"] == 3


async def test_connect_propagates_cancellation(monkeypatch):
    """Shutdown must not be swallowed by the retry loop."""

    async def cancelled(_url):
        raise asyncio.CancelledError()

    monkeypatch.setattr(runner.aio_pika, "connect_robust", cancelled)
    monkeypatch.setattr(runner, "CONNECT_RETRY_DELAY", 0.0)

    with pytest.raises(asyncio.CancelledError):
        await runner._connect("inbound")


async def test_run_service_uses_the_retrying_connect(monkeypatch):
    """``run_service`` must go through ``_connect``, not call connect_robust itself.

    Without this, a refactor could reintroduce the bug by calling
    ``aio_pika.connect_robust`` directly in ``run_service`` while ``_connect``
    stays perfectly tested but unused.
    """
    used = {"connect": 0, "raw": 0}

    async def fake_connect(_name):
        used["connect"] += 1
        raise _StopService()

    async def raw_connect(_url):  # must NOT be reached
        used["raw"] += 1
        raise AssertionError("run_service called connect_robust directly")

    monkeypatch.setattr(runner, "_connect", fake_connect)
    monkeypatch.setattr(runner.aio_pika, "connect_robust", raw_connect)

    with pytest.raises(_StopService):
        await runner.run_service("inbound")

    assert used["connect"] == 1
    assert used["raw"] == 0


class _StopService(Exception):
    """Sentinel to abort run_service right after the connect step."""


# --- run_all: one dying service must not abandon the others -------------------
async def test_run_all_reports_every_dead_service(monkeypatch):
    """gather(return_exceptions=True): all tasks are awaited, deaths are named."""
    started: list[str] = []

    async def fake_run_service(name: str):
        started.append(name)
        if name == "spt":
            raise RuntimeError("boom")
        # The survivors must still be awaited, not abandoned.
        await asyncio.sleep(0)

    monkeypatch.setattr(run_all, "run_service", fake_run_service)
    monkeypatch.setattr(run_all, "_all_services", lambda: ["inbound", "spt", "jam"])

    failed = await run_all._run()

    # Every service ran (none abandoned when 'spt' raised)...
    assert set(started) == {"inbound", "spt", "jam"}
    # ...and all three are reported: 'spt' raised, the other two returned early
    # (a consume loop returning at all is a death).
    assert set(failed) == {"inbound", "spt", "jam"}


async def test_run_all_reports_no_failures_when_cancelled(monkeypatch):
    """A cancelled service is shutdown, not a failure."""

    async def fake_run_service(_name: str):
        raise asyncio.CancelledError()

    monkeypatch.setattr(run_all, "run_service", fake_run_service)
    monkeypatch.setattr(run_all, "_all_services", lambda: ["inbound", "jam"])

    failed = await run_all._run()
    assert failed == []


@pytest.mark.asyncio(loop_scope="function")
async def test_main_exits_non_zero_when_a_service_dies(monkeypatch):
    """The orchestrator needs a real exit status — a wedged process gave none."""

    def fake_run(coro):
        coro.close()  # we replace the whole run; don't leave it un-awaited
        return ["spt", "jam"]

    monkeypatch.setattr(run_all.asyncio, "run", fake_run)
    assert run_all.main() == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_main_exits_zero_on_clean_shutdown(monkeypatch):
    def fake_run(coro):
        coro.close()
        return []

    monkeypatch.setattr(run_all.asyncio, "run", fake_run)
    assert run_all.main() == 0
