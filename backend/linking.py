"""[5] Core Backend — alert <-> journey linking (CLAUDE.md [5]).

An ``Alert`` row's ``journey_id`` FK is nullable because an alert can arrive
(and be shown on the dashboard) *before* its journey has been assembled from
``raw.events`` — the two output queues are consumed independently. This module
fills that FK in, in both directions, using **SQL id-matching** rather than the
stitcher's in-memory alias map (the alerts consumer and the raw consumer have
separate sessions and do not share the map, so a DB match is the clean seam).

An alert belongs to a journey when they share ANY correlation id
(``event_id`` / ``order_id`` / ``cart_header_id``). By the Correlation Model an
alert's log always carries at least one id that its journey also accumulates, so
a match on any of the three is sufficient and unambiguous (``accountNumber`` is
never used — it is not unique per journey).

Two entry points, one per arrival order:

* :func:`link_alert` — a new alert arrived: attach it to an existing journey now.
* :func:`backfill_journey_alerts` — a journey was (re)assembled: attach any
  orphan alerts (``journey_id IS NULL``) that were waiting for it.

**Late-arriving alerts on an already-clustered journey.** ``backend/incidents.py``'s
``process_completion`` links a journey's alerts to its incident with a ONE-TIME
query at journey-completion time — any alert that finishes AI processing and
lands *after* that moment (routine, since alerts process concurrently, bounded
by ``ALERT_CONCURRENCY``) would otherwise get its ``journey_id`` set correctly
by this module but never have its ``incident_id`` backfilled, permanently
invisible to that journey's incident (live-testing discovery). So both entry
points here also check whether the matched journey already has an incident and,
if so, backfill the alert's ``incident_id`` and bump the incident's
``alert_count``/``last_ts`` to match — returning the updated ``Incident`` row(s)
so the caller can broadcast ``incident.updated`` AFTER its own commit (mirrors
how ``alert.new`` is only broadcast once the insert is durable).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, or_, select, update

from backend.db import Alert, Incident, Journey


def _alert_matches_journey_cols(event_id, order_id, cart_header_id):
    """A WHERE clause: a row's ids match the given (non-null) journey/alert ids.

    Only non-null ids are considered — a null id must never match another null
    (that would link unrelated orphan alerts to unrelated journeys).
    """
    clauses = []
    if event_id is not None:
        clauses.append(Alert.event_id == event_id)
    if order_id is not None:
        clauses.append(Alert.order_id == order_id)
    if cart_header_id is not None:
        clauses.append(Alert.cart_header_id == cart_header_id)
    return or_(*clauses) if clauses else None


async def _bump_incident_alert_count(session, incident_id: str, *, by: int = 1):
    """Bump an incident's ``alert_count``/``last_ts`` for ``by`` late-arriving
    alerts; returns the updated row, or ``None`` if the incident is gone."""
    now = datetime.now(timezone.utc)
    stmt = (
        update(Incident)
        .where(Incident.incident_id == incident_id)
        .values(alert_count=Incident.alert_count + by, last_ts=now)
        .returning(Incident)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def link_alert(session, alert):
    """Set ``alert``'s ``journey_id`` to the journey sharing one of its ids.

    Called right after an alert is persisted. If no journey exists yet, the FK
    stays null and :func:`backfill_journey_alerts` will attach it once the
    journey is assembled. Matches on any of the alert's non-null ids.

    Returns the ``Incident`` row if this alert just backfilled onto an
    already-clustered journey's incident (caller should broadcast
    ``incident.updated`` once its own transaction commits), else ``None``.
    """
    log = alert.log
    ids = [i for i in (log.eventId, log.orderId, log.cartHeaderId) if i is not None]
    if not ids:
        return None  # no correlation ids on this log — nothing to link

    # Find a journey whose event_id/order_id/cart_header_id equals one of the
    # alert's ids. There is at most one such journey (ids are unique to a
    # journey), so a single UPDATE keyed on alert_id is enough.
    journey_match = or_(
        *[
            col == v
            for col, v in (
                (Journey.event_id, log.eventId),
                (Journey.order_id, log.orderId),
                (Journey.cart_header_id, log.cartHeaderId),
            )
            if v is not None
        ]
    )
    jid_subq = select_first_journey_id(journey_match)
    incident_subq = (
        select(Journey.incident_id).where(journey_match).limit(1).scalar_subquery()
    )
    stmt = (
        update(Alert)
        .where(Alert.alert_id == alert.alert_id, Alert.journey_id.is_(None))
        .values(journey_id=jid_subq, incident_id=incident_subq)
        .returning(Alert.incident_id)
    )
    result = await session.execute(stmt)
    row = result.first()
    incident_id = row[0] if row is not None else None
    if incident_id is None:
        return None
    return await _bump_incident_alert_count(session, incident_id)


async def backfill_journey_alerts(session, journeys) -> list:
    """Attach orphan alerts (journey_id IS NULL) to the given journeys.

    Called after journeys are upserted. For each journey, any alert that shares
    one of its ids and isn't yet linked is set to this journey. Idempotent —
    already-linked alerts are excluded by the ``journey_id IS NULL`` guard.

    Returns the list of ``Incident`` rows that absorbed one or more backfilled
    alerts (a journey can already have an incident here if it was completed —
    and clustered — before this batch of orphan alerts caught up), for the
    caller to broadcast ``incident.updated`` for each AFTER its own commit.
    """
    updated_incidents = []
    for journey in journeys:
        match = _alert_matches_journey_cols(
            journey.event_id, journey.order_id, journey.cart_header_id
        )
        if match is None:
            continue

        result = await session.execute(
            select(Journey.incident_id).where(Journey.journey_id == journey.journey_id)
        )
        row = result.first()
        incident_id = row[0] if row is not None else None

        values = {"journey_id": journey.journey_id}
        if incident_id is not None:
            values["incident_id"] = incident_id
        stmt = (
            update(Alert)
            .where(and_(Alert.journey_id.is_(None), match))
            .values(**values)
        )
        result = await session.execute(stmt)
        if incident_id is not None and result.rowcount:
            incident = await _bump_incident_alert_count(
                session, incident_id, by=result.rowcount
            )
            if incident is not None:
                updated_incidents.append(incident)
    return updated_incidents


def select_first_journey_id(journey_match):
    """Scalar subquery: the journey_id of a journey matching ``journey_match``.

    Kept as a helper so :func:`link_alert` reads cleanly; returns at most one id.
    """
    return (
        select(Journey.journey_id).where(journey_match).limit(1).scalar_subquery()
    )
