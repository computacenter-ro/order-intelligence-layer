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
"""

from __future__ import annotations

from sqlalchemy import and_, or_, update

from backend.db import Alert, Journey


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


async def link_alert(session, alert) -> None:
    """Set ``alert``'s ``journey_id`` to the journey sharing one of its ids.

    Called right after an alert is persisted. If no journey exists yet, the FK
    stays null and :func:`backfill_journey_alerts` will attach it once the
    journey is assembled. Matches on any of the alert's non-null ids.
    """
    log = alert.log
    ids = [i for i in (log.eventId, log.orderId, log.cartHeaderId) if i is not None]
    if not ids:
        return  # no correlation ids on this log — nothing to link

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
    stmt = (
        update(Alert)
        .where(Alert.alert_id == alert.alert_id, Alert.journey_id.is_(None))
        .values(journey_id=jid_subq)
    )
    await session.execute(stmt)


async def backfill_journey_alerts(session, journeys) -> None:
    """Attach orphan alerts (journey_id IS NULL) to the given journeys.

    Called after journeys are upserted. For each journey, any alert that shares
    one of its ids and isn't yet linked is set to this journey. Idempotent —
    already-linked alerts are excluded by the ``journey_id IS NULL`` guard.
    """
    for journey in journeys:
        match = _alert_matches_journey_cols(
            journey.event_id, journey.order_id, journey.cart_header_id
        )
        if match is None:
            continue
        stmt = (
            update(Alert)
            .where(and_(Alert.journey_id.is_(None), match))
            .values(journey_id=journey.journey_id)
        )
        await session.execute(stmt)


def select_first_journey_id(journey_match):
    """Scalar subquery: the journey_id of a journey matching ``journey_match``.

    Kept as a helper so :func:`link_alert` reads cleanly; returns at most one id.
    """
    from sqlalchemy import select

    return (
        select(Journey.journey_id).where(journey_match).limit(1).scalar_subquery()
    )
