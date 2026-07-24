"""[5] Core Backend — generic cursor-based (keyset) pagination.

Reusable "load more" pagination for any ``ORDER BY <sort> DESC, <id> DESC``
listing. Keyset (a.k.a. seek) beats OFFSET/LIMIT for feeds: the page after a
cursor is a single indexed range scan whose cost does not grow with how far
down the list you are, and it is immune to the row-shifting that makes OFFSET
skip/repeat rows when new alerts arrive between requests.

The three moving parts, all pure and unit-testable:

* :func:`encode_cursor` / :func:`decode_cursor` — opaque, url-safe token
  carrying the ``(sort_value, id_value)`` of the last row of a page. datetimes
  round-trip through ISO-8601.
* :func:`apply_keyset` — turn a filter-only ``Select`` into a paginated one:
  the ``DESC, DESC`` ordering, the ``(sort, id) < (cursor_sort, cursor_id)``
  seek predicate, and ``LIMIT limit + 1`` (the extra row is how we learn a
  further page exists without a second COUNT query).
* :func:`build_page` — split the ``limit + 1`` rows into the visible page plus
  the ``next_cursor`` (or ``None`` when this was the last page).

Importing this module performs no I/O.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import Select, tuple_
from sqlalchemy.orm import InstrumentedAttribute


def encode_cursor(sort_value: Any, id_value: Any) -> str:
    """Serialize ``(sort_value, id_value)`` into an opaque url-safe token.

    datetimes are stored as ISO-8601 strings (the only non-JSON-native type we
    sort on); everything else is passed through as-is. The result is
    url-safe-base64 so it drops straight into a query string.
    """
    payload = {
        "sort_value": sort_value.isoformat() if isinstance(sort_value, datetime) else sort_value,
        "id_value": id_value,
    }
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> tuple[Any, Any]:
    """Inverse of :func:`encode_cursor` → ``(sort_value, id_value)``.

    A ``sort_value`` that parses as an ISO-8601 datetime is rehydrated to a
    ``datetime`` (via :meth:`datetime.fromisoformat`) so the seek predicate
    compares like-for-like against a ``DateTime`` column; any other string is
    left untouched.
    """
    raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    payload = json.loads(raw)
    sort_value = payload["sort_value"]
    if isinstance(sort_value, str):
        try:
            sort_value = datetime.fromisoformat(sort_value)
        except ValueError:
            pass  # a non-datetime sort key (e.g. a plain string) — keep as-is
    return sort_value, payload["id_value"]


def apply_keyset(
    stmt: Select,
    sort_col: InstrumentedAttribute,
    id_col: InstrumentedAttribute,
    *,
    cursor: str | None,
    limit: int,
    nulls_last: bool = False,
) -> Select:
    """Turn a filter-only ``Select`` into a keyset-paginated one.

    Orders by ``sort_col DESC, id_col DESC`` (``id_col`` is the tiebreak that
    makes the order total, so the cursor is unambiguous even when many rows
    share a ``sort_col`` value). When ``cursor`` is given, appends the seek
    predicate ``(sort_col, id_col) < (sort_val, id_val)`` — a row-value
    comparison that means "strictly after the cursor row in this ordering".
    Fetches ``limit + 1`` rows: the surplus row (if present) tells
    :func:`build_page` there is a further page.

    ``nulls_last`` puts NULL ``sort_col`` values at the end of the DESC order
    (Postgres defaults NULLs first on DESC), for nullable sort columns such as
    ``journeys.last_ts``.
    """
    sort_order = sort_col.desc()
    if nulls_last:
        sort_order = sort_order.nulls_last()
    stmt = stmt.order_by(sort_order, id_col.desc())
    if cursor is not None:
        sort_val, id_val = decode_cursor(cursor)
        stmt = stmt.where(tuple_(sort_col, id_col) < (sort_val, id_val))
    return stmt.limit(limit + 1)


def build_page(
    rows: list[Any],
    limit: int,
    sort_key_getter: Callable[[Any], Any],
    id_getter: Callable[[Any], Any],
) -> tuple[list[Any], str | None]:
    """Split ``limit + 1`` fetched rows into ``(page, next_cursor)``.

    If more than ``limit`` rows came back, a further page exists: trim to
    ``limit`` and mint ``next_cursor`` from the last *kept* row (so the next
    request seeks strictly past it). Otherwise this was the final page and
    ``next_cursor`` is ``None``.
    """
    if len(rows) > limit:
        page = rows[:limit]
        last = page[-1]
        return page, encode_cursor(sort_key_getter(last), id_getter(last))
    return list(rows), None
