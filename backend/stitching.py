"""Single-pass, incremental journey stitching (pure logic).

This module owns the Correlation Model's assembly algorithm and *nothing else*
— no database, no RabbitMQ, no I/O. It is deliberately trivial to unit-test:
feed it ``LogLine`` objects, read back assembled journeys.

See CLAUDE.md "THE CORRELATION MODEL" for the invariants this relies on. The
short version: a journey has **no single id present on every log**. The
identifier changes over the journey's lifetime —

* **phase 1** logs carry only ``eventId`` (as a field);
* the **bridge** log (order-creation-response ack) carries only ``eventId`` —
  it no longer links the id families;
* **phase 2** logs carry both order ids (as fields) and never ``eventId``.

The eventId->order-id join is closed by **id mining**: the order-engine
creation logs carry ``eventId`` as a field AND the freshly minted order ids in
their *message text* (e.g. "Created cart header 1840927365018240001",
"Generated order number ORD-6001 for cart header 1840927365018240001"). We
extract ids from ``log.message`` with strict, word-boundary patterns and treat
them EXACTLY like structured-field ids — registering them in the same alias map.
So a single pass over the logs **in timestamp order** correlates every log:
those creation logs (which sit before the bridge) tie eventId to the order ids,
and every later phase-2 log shares an order id already known. No second pass and
no back-patching. Because the creation-log *text* now carries correlation
information, that text is **load-bearing** (like the terminal messages the
journey-over rules match on): changing it requires updating these patterns.

``accountNumber`` is NEVER consulted — neither as a field nor mined from text:
it is not unique per journey (see the schema table in CLAUDE.md) and using it
would merge unrelated orders. Mining is likewise scoped to the three id families
only.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from shared.models import LogLine

# The correlation id fields, in the priority order used to pick a journey_id.
# accountNumber is intentionally absent — see module docstring.
_ID_FIELDS = ("eventId", "orderId", "cartHeaderId")

# Strict, word-boundary patterns for mining ids from a log's MESSAGE TEXT. They
# mirror the id shapes in CLAUDE.md's log schema exactly:
#   eventId       evt-<hex/dashes>      orderId  ORD-<digits>   cartHeaderId  19 digits
# The 19-digit cart pattern uses \b on both sides so an unrelated longer/shorter
# run of digits (or a number embedded in a larger token) never matches — a stray
# 19-digit number in prose is the only false-positive risk and is vanishingly
# unlikely, whereas a 12- or 20-digit number is rejected outright.
_MINE = {
    "eventId": re.compile(r"evt-[0-9a-f-]{8,}"),
    "orderId": re.compile(r"\bORD-\d+\b"),
    "cartHeaderId": re.compile(r"\b\d{19}\b"),
}


def _collect_ids(log: LogLine) -> dict[str, str]:
    """All correlation ids on a log: structured fields first, then mined from
    ``message`` text. Returns ``{family: id}`` (one per family; a field value
    wins over a mined one if both are present, though they agree in practice).

    Mined ids are treated identically to field ids by the caller — they become
    journey aliases and populate the journey's event_id/order_id/cart_header_id.
    """
    ids: dict[str, str] = {}
    for family in _ID_FIELDS:
        value = getattr(log, family)
        if value is not None:
            ids[family] = value
    message = log.message or ""
    for family, pattern in _MINE.items():
        if family not in ids:  # a structured field takes precedence
            match = pattern.search(message)
            if match:
                ids[family] = match.group(0)
    return ids


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

        # Ids come from BOTH structured fields and mined message text, treated
        # identically (see _collect_ids / module docstring).
        id_map = _collect_ids(log)
        ids = list(id_map.values())

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
        self._absorb_ids(journey, id_map)

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
    def _absorb_ids(journey: StitchedJourney, id_map: dict[str, str]) -> None:
        """Copy this log's ids (fields + mined) onto the journey's alias fields.

        ``id_map`` is the {family: id} from :func:`_collect_ids`, so mined ids
        land in ``event_id`` / ``order_id`` / ``cart_header_id`` exactly like
        structured-field ids — the DB columns, WS payloads and Teams links that
        read these journey fields therefore see mined ids transparently.
        """
        if "eventId" in id_map:
            journey.event_id = id_map["eventId"]
        if "orderId" in id_map:
            journey.order_id = id_map["orderId"]
        if "cartHeaderId" in id_map:
            journey.cart_header_id = id_map["cartHeaderId"]

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
