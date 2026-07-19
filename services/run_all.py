"""Start every mock service baton consumer in one process (CLAUDE.md [1]).

Each service is normally ``python -m services.runner <service>``; this launches
all of them concurrently on one event loop so a single command brings the whole
emitter tier up::

    python -m services.run_all

The set of services is derived from ``shared.scenarios`` (the services that
appear in any compiled step chain), so it stays in sync with the scenarios.
"""
from __future__ import annotations

import asyncio
import sys

from services.runner import run_service
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


async def _run() -> None:
    services = _all_services()
    print(f"[run_all] starting {len(services)} services: {', '.join(services)}", flush=True)
    # One task per service; each runs its own consume loop forever. If one dies,
    # surface it but keep the others alive.
    tasks = [asyncio.create_task(run_service(svc), name=svc) for svc in services]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:  # pragma: no cover - Ctrl-C path
        for task in tasks:
            task.cancel()
        raise


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover
        print("\n[run_all] shutting down", flush=True)


if __name__ == "__main__":
    main()
