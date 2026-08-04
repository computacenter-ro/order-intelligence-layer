"""rename the "general" department to "business" (alerts + incidents)

Revision ID: e2b6d4a9f157
Revises: c9b3f07a51de
Create Date: 2026-08-03 09:41:28.115204

``Department.general`` meant "not an engineering fault — the pipeline worked as
designed and correctly rejected the order". Once the Teams channel split gave
those alerts their own ``business`` channel the name was actively misleading (the
dashboard rendered "General" for an alert whose card went to ``#business-logs``),
and it is semantically empty besides. Renamed end to end; the other four
departments are untouched.

``department`` is a plain ``String`` column on both tables (``backend/db.py``),
NOT a native PG enum, so there is no type to ALTER — the data update IS the
migration:

    UPDATE alerts    SET department = 'business' WHERE department = 'general';
    UPDATE incidents SET department = 'business' WHERE department = 'general';

``downgrade`` is exactly symmetric. It is lossless in both directions here only
because ``business`` did not previously exist as a department: nothing legitimately
held that value before this migration, so mapping it back cannot capture a row
that was always ``business``. (If ``business`` is ever reused for a *different*
meaning, this stops being true — write a new revision rather than editing this
one.)

⚠ NOT renamed by this migration, deliberately: the ``general`` Teams **channel**
(``backend/teams.py``'s ``GENERAL`` / ``TEAMS_WEBHOOK_GENERAL``), which carries
journey completions and never corresponded to a department. No column stores it.

===============================================================================
Operational notes — read before deploying
===============================================================================

**1. In-flight ``processed.alerts`` messages (the one that needs a decision).**

That queue is durable and at-least-once, so messages minted by the AI service
before the deploy carry ``department="general"``, which the new enum rejects at
parse time. ``AlertsConsumer._decode`` calls
``ProcessedAlert.model_validate_json``, which raises; ``_QueueConsumer._on_message``
treats a decode failure as a **poison message — acked and dropped**, with a log
line naming the offending field and value. So the failure mode is already bounded:
never a crash-loop, never an unbounded redelivery loop, and never truly silent.
It IS a lost alert though — no row, no Teams card, no incident membership.

**Chosen: drain the queue, do not ship a read-time compatibility shim.**

    1. Stop the AI service (it is the only publisher).
    2. Wait for ``processed.alerts`` depth to reach 0 — RabbitMQ management UI, or
       ``rabbitmqctl list_queues name messages | grep processed.alerts``.
    3. Run this migration, then start both services on the new code.

Why not accept the legacy value on read for one release: a
``"general" -> business`` alias on the enum would keep the misleading value
parseable, and a "remove it next release" shim is exactly the kind of thing that
never gets removed — leaving the system able to accept forever the value this
rename exists to eliminate. Draining costs one ordered restart and leaves no
residue. The exposure if someone skips step 2 is only the alerts published in the
seconds before the backend restarts, each one logged by name.

Note the compose files already order this correctly for the DB itself: ``backend``
depends on ``migrate`` completing successfully, so the rows are converted before
anything reads them.

**2. Redis ``ai:semcache``** holds cached payloads with ``"general"``.
``ai_service/graph.py::_alert_from_payload`` re-validates the department against
the enum, catches the ``ValueError`` and returns ``None``, so those
entries degrade to cache MISSES and are re-cached with the new value —
self-healing, no crash. Expect a burst of LLM calls after the deploy as the cache
refills. ``DEL ai:semcache`` makes it clean immediately (the key is rebuildable;
dropping it is always safe).

**3. Redis ``ai:ragindex``** metadata carries ``department`` for filtering, so
records indexed before the deploy will not match a ``department=business`` filter
(they still match unfiltered retrieval — this degrades recall on a filtered query,
it does not corrupt anything). Fix by re-running
``python -m backend.scripts.backfill_rag``, which is idempotent and upserts by
record id.

**4. Persisted dashboard filters** are handled in the frontend, not here:
``AlertFilterBar`` sanitises ``oil.alertFilters`` / ``oil.historyFilters`` against
the known department list on load, dropping unknown values. Without that a user
who had ``general`` ticked would send ``?department=general`` and get a 422 from
the enum-typed query param — a broken feed with no obvious cause.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e2b6d4a9f157'
down_revision: Union[str, Sequence[str], None] = 'c9b3f07a51de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename the department value on both tables that store one."""
    op.execute(
        "UPDATE alerts SET department = 'business' WHERE department = 'general'"
    )
    op.execute(
        "UPDATE incidents SET department = 'business' WHERE department = 'general'"
    )


def downgrade() -> None:
    """Exactly symmetric — see the module docstring on why this is lossless."""
    op.execute(
        "UPDATE alerts SET department = 'general' WHERE department = 'business'"
    )
    op.execute(
        "UPDATE incidents SET department = 'general' WHERE department = 'business'"
    )
