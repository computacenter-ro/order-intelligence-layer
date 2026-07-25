"""Start every mock service baton consumer in one process (CLAUDE.md [1]).

Each service is normally ``python -m pipeline.services.runner <service>``; this
launches all of them concurrently on one event loop so a single command brings
the whole emitter tier up::

    python -m pipeline.services.run_all

The set of services is derived from ``shared.scenarios`` (the services that
appear in any compiled step chain), so it stays in sync with the scenarios.
"""
from __future__ import annotations

import asyncio
import sys

from pipeline.services.runner import run_service
from shared.scenarios import all_scenarios, compile_steps

# The runner's diagnostic prints use a few non-ASCII glyphs (arrows, em-dashes).
# On Windows the default console codepage (cp1252) can't encode them and the
# print raises UnicodeEncodeError mid-dispatch. Force UTF-8 stdout/stderr so the
# emitter tier runs on any console without a PYTHONIOENCODING dance.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass


def _all_services() -> list[str]:
    """Every distinct service that appears as a step[0] across all scenarios."""
    services: list[str] = []
    for scenario in all_scenarios():
        for service, _block in compile_steps(scenario):
            if service not in services:
                services.append(service)
    return services


async def _run() -> list[str]:
    """Run every service concurrently; return the names that died.

    ``return_exceptions=True`` is load-bearing. Without it the FIRST task to raise
    makes ``gather`` propagate immediately and the other nine are abandoned
    mid-flight — they keep looping with nobody awaiting them, which is how this
    process used to end up alive but not consuming (a container that looks healthy
    to Docker while draining no queues). With it, every task is awaited, one
    service's failure is contained, and the caller can decide deliberately.
    """
    services = _all_services()
    print(f"[run_all] starting {len(services)} services: {', '.join(services)}", flush=True)
    # One task per service; each runs its own consume loop forever. If one dies,
    # surface it but keep the others alive.
    tasks = [asyncio.create_task(run_service(svc), name=svc) for svc in services]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:  # pragma: no cover - Ctrl-C path
        for task in tasks:
            task.cancel()
        raise

    # A consume loop is supposed to run forever, so ANY return here is a death —
    # an exception or an unexpected clean exit. Name each one; the broker-unreachable
    # case can no longer appear (runner._connect retries), so anything landing here
    # is a real bug worth a non-zero exit and a restart.
    failed: list[str] = []
    for service, result in zip(services, results):
        if isinstance(result, asyncio.CancelledError):
            continue  # shutdown, not a failure
        if isinstance(result, BaseException):
            print(
                f"[run_all] service {service!r} died: "
                f"{type(result).__name__}: {result}",
                flush=True,
            )
        else:
            print(f"[run_all] service {service!r} exited unexpectedly", flush=True)
        failed.append(service)
    return failed


def main() -> int:
    """Exit non-zero if any service died, so ``restart: on-failure`` can act.

    The old code could leave the process alive with dead consumers, so Docker saw
    a healthy container and never restarted it. Returning a real exit status makes
    the failure visible to the orchestrator (compose restart policy today, a
    Kubernetes liveness probe later).
    """
    try:
        failed = asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover
        print("\n[run_all] shutting down", flush=True)
        return 0
    if failed:
        print(f"[run_all] exiting non-zero: {len(failed)} service(s) died: {', '.join(failed)}",
              flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
