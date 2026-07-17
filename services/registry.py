"""Shared ``(service, block)`` → handler registry for the mock services.

Why this lives in its own module (not in ``runner.py``): the runner is launched
with ``python -m services.runner <svc>``, which loads ``runner.py`` as the
``__main__`` module. A service module doing ``from services.runner import
register`` would then import ``runner.py`` **a second time** under the name
``services.runner`` — a *different* module object with its own ``BLOCKS`` dict.
Blocks would register into one copy while the running loop reads the other,
so nothing ever dispatches.

Keeping ``BLOCKS`` + ``register`` here means there is exactly **one** registry
module object regardless of how the runner is started. Both ``runner.py`` and
every service module import ``register`` from here.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from shared.models import Baton, LogLine

# A block emits logs through this callable — the runner owns the ``LogClient``
# and hands each block its ``emit`` method so blocks never build their own
# client. Returns the collector's ``ingested`` count.
EmitFn = Callable[["LogLine | list[LogLine]"], Awaitable[int]]

# A block emits the log lines for one ``(service, block)`` step via the provided
# ``emit`` callable. It may mutate ``baton.ctx`` in place — e.g. order_engine's
# ``create`` block fills the order ids — and returns whether the chain should
# continue:
#   * ``True``  → forward the baton to the next step.
#   * ``False`` → fatal failure; stop here, do NOT forward.
Block = Callable[[Baton, EmitFn], Awaitable[bool]]

# Registry populated by service modules via ``@register``: {(service, block): coroutine}.
BLOCKS: dict[tuple[str, str], Block] = {}


def register(service: str, block: str) -> Callable[[Block], Block]:
    """Decorator registering a coroutine as the handler for one ``(service, block)`` step."""

    def decorator(fn: Block) -> Block:
        BLOCKS[(service, block)] = fn
        return fn

    return decorator
