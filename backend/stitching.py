"""Single-pass, incremental journey stitching (pure logic).

This module owns the Correlation Model's assembly algorithm and *nothing else*
— no database, no RabbitMQ, no I/O. It is deliberately trivial to unit-test:
feed it ``LogLine`` objects, read back assembled journeys.

See CLAUDE.md "THE CORRELATION MODEL" for the invariants this relies on. The
short version: a journey has **no single id present on every log**. The
identifier changes over the journey's lifetime —

* **phase 1** logs carry only ``eventId``;
* the **bridge** log carries ``eventId`` *and* >=1 order id;
* **phase 2** logs carry both order ids and never ``eventId``.

Crucially, every log that introduces a *new* id also carries an
*already-known* id (the bridge shares ``eventId``; the first phase-2 log shares
an order id the bridge exposed). So a single pass over the logs **in timestamp
order**, maintaining an ``id -> journey_id`` alias map, correlates every log
correctly for all three bridge variants (both / order-only / cart-only) — no
second pass and no back-patching required.

``accountNumber`` is NEVER consulted: it is not unique per journey (see the
schema table in CLAUDE.md) and using it would merge unrelated orders.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from shared.models import LogLine

# The correlation id fields, in the priority order used to pick a journey_id.
# accountNumber is intentionally absent — see module docstring.
_ID_FIELDS = ("eventId", "orderId", "cartHeaderId")


@dataclass
class StitchedJourney:
    """An incrementally assembled journey: an internal id + accumulated logs.

    ``event_id`` / ``order_id`` / ``cart_header_id`` are the business-id aliases
    gathered over the journey's lifetime; any of them may stay ``None`` (a
    pre-creation failure lives and dies with only ``event_id``). Logs are kept
    in timestamp order.
    """

    journey_id: str
    logs: list[LogLine] = field(default_factory=list)
    event_id: str | None = None
    order_id: str | None = None
    cart_header_id: str | None = None

    @property
    def alias_ids(self) -> set[str]:
        """Every business id currently known to belong to this journey."""
        return {i for i in (self.event_id, self.order_id, self.cart_header_id) if i}

    @property
    def first_ts(self):
        return self.logs[0].timestamp if self.logs else None

    @property
    def last_ts(self):
        return self.logs[-1].timestamp if self.logs else None


class Stitcher:
    """Accumulates ``LogLine`` s into journeys across successive batches ("polls").

    Usage::

        s = Stitcher()
        s.add_batch(poll_1_logs)   # returns the journeys touched by this batch
        s.add_batch(poll_2_logs)   # a journey grows as later polls deliver more
        s.journeys                 # all journeys assembled so far

    Idempotent on ``log_id`` — the output queues are at-least-once, so a
    re-delivered log attaches at most once.
    """

    def __init__(self) -> None:
        self._aliases: dict[str, str] = {}          # business id -> journey_id
        self._journeys: dict[str, StitchedJourney] = {}
        self._seen_log_ids: set[str] = set()

    # --- public API ----------------------------------------------------------

    def add(self, log: LogLine) -> StitchedJourney | None:
        """Stitch a single log. Returns its journey, or ``None`` if it was a
        duplicate ``log_id`` (already stitched)."""
        if log.log_id in self._seen_log_ids:
            return None
        self._seen_log_ids.add(log.log_id)

        ids = [getattr(log, f) for f in _ID_FIELDS if getattr(log, f) is not None]

        # Pick the journey: first known alias wins; otherwise start a new one.
        journey_id = next((self._aliases[i] for i in ids if i in self._aliases), None)
        if journey_id is None:
            journey_id = uuid.uuid4().hex
            self._journeys[journey_id] = StitchedJourney(journey_id=journey_id)

        journey = self._journeys[journey_id]

        # Register ALL of this log's ids as aliases of the journey. Every log
        # that introduces a new id also carries a known one, so this only ever
        # extends the journey's alias set — it never links two existing ones.
        for i in ids:
            self._aliases[i] = journey_id
        self._absorb_ids(journey, log)

        self._insert_in_ts_order(journey, log)
        return journey

    def add_batch(self, logs) -> list[StitchedJourney]:
        """Stitch a batch of logs (processed in timestamp order).

        Returns the distinct journeys this batch touched, in first-touched
        order — the backend uses this to know which journeys to re-evaluate.
        """
        touched: list[StitchedJourney] = []
        for log in sorted(logs, key=lambda l: l.timestamp):
            journey = self.add(log)
            if journey is not None and journey not in touched:
                touched.append(journey)
        return touched

    @property
    def journeys(self) -> list[StitchedJourney]:
        """All journeys assembled so far, in creation order."""
        return list(self._journeys.values())

    def journey_for(self, business_id: str) -> StitchedJourney | None:
        """Return the journey a business id belongs to, or ``None``."""
        journey_id = self._aliases.get(business_id)
        return self._journeys.get(journey_id) if journey_id else None

    # --- internals -----------------------------------------------------------

    @staticmethod
    def _absorb_ids(journey: StitchedJourney, log: LogLine) -> None:
        """Copy any ids present on ``log`` onto the journey's alias fields."""
        if log.eventId is not None:
            journey.event_id = log.eventId
        if log.orderId is not None:
            journey.order_id = log.orderId
        if log.cartHeaderId is not None:
            journey.cart_header_id = log.cartHeaderId

    @staticmethod
    def _insert_in_ts_order(journey: StitchedJourney, log: LogLine) -> None:
        """Append keeping ``logs`` sorted by timestamp.

        Within a batch we already iterate in timestamp order, but a later poll
        can carry a log slightly older than one already stored (overlapping
        sliding windows), so we insert at the right position rather than blindly
        appending.
        """
        logs = journey.logs
        if not logs or log.timestamp >= logs[-1].timestamp:
            logs.append(log)
            return
        lo, hi = 0, len(logs)
        while lo < hi:
            mid = (lo + hi) // 2
            if logs[mid].timestamp <= log.timestamp:
                lo = mid + 1
            else:
                hi = mid
        logs.insert(lo, log)
