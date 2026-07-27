# Implementation plan — alert clustering into incidents

Hand-off spec for Claude Code. You have the real repo; I've written this against
CLAUDE.md + `pipeline/data/mock-order-flows-v3.json`. Where I say "confirm/wire
into", check the actual code — I may be off on exact names.

## 0. Goal

Collapse the many alerts of one failure episode into a single **incident**
(1 primary + N collapsed). For **infrastructure-class** failures (dependency
never responded — §1, §4b), also group episodes of the same failure across
different orders into one systemic incident, reducing dashboard + Teams noise
and surfacing blast radius ("SPT unreachable — 12 orders"). **Order-specific**
failures (dependency responded with a verdict about this order) always stay
scoped to their own order this iteration — see §12 for why cross-order
grouping there is deferred, not built. Layered view: incident → affected
orders → each order's alerts.

## 1. Decisions already made — do NOT re-open

- **Clustering state lives in the backend.** It needs cross-alert/cross-journey
  state, correlation-id matching, and journey outcomes — all backend-owned.
- **Episodes reuse existing journeys.** The "order" rung = the journey the
  backend already assembles. Don't build a parallel grouping.
- **The embedding is computed in ai_service and shipped on `ProcessedAlert`.**
  Reason: the encoder is already loaded there (semcache), it's local/independent
  of the Azure LLM so it survives the breaker being open, and it keeps a second
  copy of the model out of the backend process.
- **Deterministic key is primary; embedding is the fallback for the unrecognized
  tail only.** For the 10 known scenarios, `(failure_subtype, failing_service)`
  from structured fields fully classifies — the embedding is NOT consulted there.
- **Signature is derived from the causal line, never the generic abort line**
  (see §5), read from the journey's already-linked `alerts`, never raw
  `journey_events` (see §5 — this is why the backend needs no suppression-list
  copy of its own).
- **Must keep working with the LLM down** (attach embedding on both `ai` and
  `fallback` paths; deterministic keying needs no LLM).
- **`TIMED_OUT` journeys with a real ERROR-level alert are incident-eligible
  too, via the novel/embedding path only** (§4, §10). Without this, a genuinely
  unrecognized failure type — the entire reason embeddings were chosen over a
  hardcoded list — never clusters: `journeys.py`'s hardcoded `_FAILURE_RULES`
  gate `FAILED` classification, so an unrecognized fatal message never reaches
  `FAILED`, only `TIMED_OUT`. Excluding `TIMED_OUT` (the plan's original
  default) would silently defeat the reason for using embeddings at all.
- **The embedding is NOT a free by-product of the semantic cache.** `semcache.
  lookup()`/`store()` never return a vector to their caller, and the common
  exact-match cache-hit path skips `encode()` entirely. A new explicit
  `semcache.embed()` call (reusing the loaded encoder + `normalize()`) computes
  one vector per alert — a deliberate, bounded extra cost, not a reuse of an
  already-computed value (see §7).
- **Failures split into INFRA vs order-specific, and only INFRA spans orders**
  (§4b, §5). INFRA = the dependency never responded (a timeout/connection
  failure) — order-independent, so it freely clusters across orders.
  Order-specific = the dependency responded with a verdict about THIS order's
  own data (margin below threshold, missing UDF, account disabled, no SKU
  mapping) — these do NOT correlate across orders and **always stay per-order
  this iteration, with no escalation path** (a multiplicity-based escalation
  to a systemic incident is deferred — see §12 Future Improvements).
  Classification is a deterministic map from the mined error token, not ML,
  and applies on both the recognized and novel/embedding paths.
- **Incidents close ONLY on a manual resolve (dashboard) or the quiet-gap
  timer (`INCIDENT_QUIET_TIMEOUT`) as a safety net — no automatic "recovery"
  detection, for any incident.** A same-`failing_service` SUCCESS on an
  unrelated order proves nothing for an order-specific/business-rule failure
  (order B's margin passing says nothing about order A's margin issue) — so
  it is never used as a signal.

## 2. Architecture / where code goes

- **ai_service**: attach a per-alert embedding to every `ProcessedAlert`
  (both `ai` and `fallback` sources), off the fetch path, via the new
  `semcache.embed()` call (§1, §7) — a real per-alert cost, not free reuse.
- **backend**: new incident clustering stage in the consumer path, keyed off
  journey completion (a journey resolves → its episode joins/opens an incident).
  New `incidents` table + FKs. New WS/Teams events. New API reads.
- Do NOT touch the mock services, the collector, the poller window logic, or
  stitching's correlation model.

## 3. Data model & contract changes

**`shared/models.py`** — add to `ProcessedAlert`:
- `embedding: list[float] | None` (the masked-message vector; `None` allowed).

**`processed.alerts` payload** — carry the new field (it's just serialized
`ProcessedAlert`; confirm the publisher/consumer round-trips it).

**Backend schema (new migration):**
```
incidents(
  incident_id PK,
  signature TEXT,            -- (failure_subtype, failing_service[, error_token]) hash
  failure_subtype TEXT,
  failing_service TEXT NULL,
  error_token TEXT NULL,
  title TEXT,               -- synthesized deterministically; regenerated on update
  department TEXT NULL,      -- for Teams routing; majority of member alerts, else general
  status TEXT,               -- open | resolved
  first_ts, last_ts TIMESTAMPTZ,
  primary_alert_id FK NULL,   -- set once at creation, from the founding causal
                              -- alert; never reassigned (see §4 step 6). This
                              -- IS the novel-path comparison target, not just
                              -- a UI click-through — see below.
  alert_count INT,
  journey_count INT          -- distinct journeys = blast radius
)
alerts.incident_id   FK NULL   -- new
journeys.incident_id FK NULL   -- new (the middle rung for the layered view)
```
- Store `embedding` on `alerts` too (durable per-alert value — the cache's is
  evicted/collapsed and unusable). **Column type: plain JSON/array (decided).**
  `pgvector` can be revisited later if brute-force cosine over open incidents
  ever becomes a real bottleneck — not a concern at this scale.
- **`incidents` needs NO embedding column of its own.** The novel path's
  "cosine ≥ threshold" (§4 step 5) compares a new causal alert's embedding
  against `alerts.embedding` for the row at `incidents.primary_alert_id` — a
  join, not a duplicated/stored vector. This is why `primary_alert_id` is set
  once at creation and never reassigned (§4 step 6): it has to stay a stable
  comparison target, not just point at "whichever alert is currently prettiest
  for the UI."
- All datetimes `timestamptz`, tz-aware (house rule).

## 4. The clustering mechanism

**Eligibility.** Operate only on non-suppressed WARN/ERROR alerts. SUCCESS →
none. FAILED journeys form incidents via the deterministic-or-novel path below.
**`TIMED_OUT` journeys are ALSO eligible, but ONLY via the novel/embedding
path** — never the deterministic one — and only if at least one ERROR-level
alert is linked to that journey (a `TIMED_OUT` journey with no ERROR at all,
e.g. one that just stopped mid-flow with only INFO/WARN, still forms no
incident: there is no causal line to anchor on).

Per alert / per journey-completion:

1. **Group by order id (episode).** Attach the alert to its journey via
   correlation id — existing `linking.py` (`link_alert`). This is the ONLY thing
   that groups an order's retries with its abort. No embeddings, no logger here.
2. **On journey → FAILED or TIMED_OUT-with-an-ERROR, pick the causal line** =
   first ERROR among the journey's already-**linked alerts** (fallback: first
   linked WARN if truly no ERROR — FAILED case only; TIMED_OUT requires an
   ERROR per the eligibility rule above). Reading from linked `alerts` (not
   raw `journey_events`) means every candidate here is already suppression-safe
   for free — see §5 — so there's no separate "non-suppressed" check to do at
   this step. Never the generic orchestrator abort.
3. **Build the signature** from the causal line:
   - `failing_service` from its logger (`SptClient`→SPT, `JamClient`→JAM, …;
     `app_name` for non-client failures),
   - `failure_subtype` from existing terminal detection (`journeys.py`) — **for
     a TIMED_OUT journey this is always `None` by definition** (that's exactly
     why it timed out instead of resolving to FAILED), so step 4 always routes
     it to the novel path; no other special-casing needed here,
   - optional `error_token` mined from the message (`SocketTimeoutException`,
     `HTTP 403`, `RFC_COMMUNICATION_FAILURE`, `SQLTimeoutException`).
   `signature = hash(failure_subtype, failing_service[, error_token])`.
   - **3b. If the only failing signal is a generic/orchestrator line** (no
     specific causal line — doesn't occur in the 10 scenarios but will in real
     logs), derive from its *message*: mine tokens; if unrecognized, use the
     embedding (step 4 novel path).
4. **Recognized vs novel.**
   - Recognized signature (`failure_subtype` present, i.e. the FAILED case with
     a rule match) → go to 5 with exact `signature`. Embedding NOT used.
   - Not recognized (`failure_subtype` is `None` — covers both the TIMED_OUT
     case and a FAILED case with an unrecognized causal line) → novel path: use
     the causal alert's embedding as the episode's representative vector. If
     `failing_service` also can't be derived (a fully generic/unrecognized
     causal logger), drop the service guard in step 5 and rely on cosine +
     the salient-token veto alone.
4b. **Classify INFRA vs order-specific** (see §5 for the token table). Applies
   regardless of whether step 4 landed on the recognized or novel path:
   - **INFRA** (dependency never responded — timeout/connection-class token) →
     always eligible to span orders; go to 5 as normal.
   - **Order-specific** (dependency responded with a verdict — business-rule
     class token) → stays **per-order, permanently, this iteration**: this
     order's failure only ever joins/opens an incident scoped to its own
     journey, never matched against another order's, no matter how many other
     orders end up with the same signature. (A multiplicity-based escalation
     to a cross-order systemic incident was considered and deferred — see §12.)
   - **A token matching neither shape (genuinely unseen) defaults to
     order-specific, never INFRA.** The token table in §5 is itself a
     deterministic shape-matcher (timeout/connection/5xx vs
     4xx/validation/"not found"), so it has the same blind spot as any
     hardcoded list for a truly novel error shape. Defaulting the unknown case
     to "don't merge across orders" is the safe direction: at worst, a genuine
     cross-order outage with an unrecognized token shows up as several
     separate per-order incidents instead of one — noisier, but not
     misleading. Defaulting the other way (unknown → INFRA) risks an
     immediate false merge of unrelated orders, which actively implies a
     shared root cause that doesn't exist.
5. **Assign to an incident (cross-order or per-order per 4b).** Find an OPEN
   incident matching:
   - Recognized + INFRA: same `signature` (exact equality).
   - Novel + INFRA: same `failing_service` (hard guard) AND **cosine ≥
     threshold between the new causal alert's embedding and
     `alerts.embedding` for the candidate incident's `primary_alert_id`** (the
     founding member's vector — see §3; not a stored/centroid vector on
     `incidents` itself) AND passes the salient-token veto (reuse semcache
     `diverges()` if available).
   - Order-specific (recognized or novel): scoped to this journey only — no
     cross-order lookup at all.
   Match → join; else open a new incident (or per-order episode).
6. **Update incident state.** Bump `journey_count` (distinct journeys) and
   `alert_count`, advance `last_ts`, refresh `title` (`failure_subtype` +
   `failing_service` + count). **Set `primary_alert_id` only when the
   incident is first created (the founding causal alert); never reassign it
   on later joins** — it must stay a stable comparison target for the novel
   path's cosine check (§3, §4 step 5), not just track "the newest alert."
   Idempotent on `journey_id`/`alert_id` (unique) so at-least-once redelivery
   can't double-count. Reuse/extend the `backfill_journey_alerts` pattern for
   late-arriving alerts.
7. **Lifecycle.** Keep open while matching failures arrive (each new member
   advances `last_ts`). **Closes ONLY on a manual resolve from the dashboard,
   or the quiet-gap timer (`INCIDENT_QUIET_TIMEOUT`) as a fallback** — no
   automatic "recovery" detection of any kind, for any incident class (see §1
   for why a same-service SUCCESS is not a valid signal here). The quiet gap is
   a rolling window (no new member for `INCIDENT_QUIET_TIMEOUT` seconds), NOT a
   fixed clock bucket, and is deliberately generous since it's a safety net, not
   the primary mechanism. A later matching failure after a close always opens a
   fresh incident, never reopens the old one. Add a sweep like the existing
   `STALLED_SWEEP_INTERVAL` stalled-journey sweep.

Title is synthesized **deterministically** (no LLM), so it reads correctly with
the breaker open. Optionally upgrade wording via the existing
`/summarize-journey` path with a template fallback.

## 5. Causal-line selection rule (with evidence from v3)

Causal line = **first ERROR among the journey's linked `alerts`** (fallback:
first linked WARN — FAILED case only, see §4 step 2). This is always the
specific component logger, never the shared orchestrator
`c.c.orderengine.service.OrderProcessingService` abort, which is always last
(and in scenario 6 is only a WARN).

| Scenario | Causal line (first ERROR among linked alerts) → failing_service | subtype |
|---|---|---|
| 4 | `cc-inbound-service` · `…transform.TransformService` → inbound-transform | INBOUND_TRANSFORM_FAILED |
| 5 | `cc-order-engine` · `…service.OrderCreationService` → BM-DB/creation | ORDER_CREATION_FAILED |
| 6 | `cc-checker-service` · `…checker.service.MarginCheckService` → checker | MARGIN_CHECK_FAILED |
| 7 | `cc-validator-service` · `…strategy.ValidateOrderLineUdfFields` → validator | VALIDATION_FAILED |
| 8 | `cc-order-engine` · `…client.SptClient` → SPT | ENRICHMENT_FAILED |
| 9 | `cc-order-engine` · `…client.JamClient` → JAM | AUTH_FAILED |
| 10 | `cc-outbound-osw` · `…client.SapRfcClient` → SAP | SAP_SUBMISSION_FAILED |

No separate backend suppression list is needed. Because the causal line is
picked from the journey's linked `alerts` (§4 step 2) rather than raw
`journey_events`, a suppressed line (`"Not implemented"`, `"No internal
contracts found"`) never became an `Alert` row in the first place — ai_service's
suppression gate already removed it upstream, before the causal-line search
ever sees it. Nothing to keep in sync.

### INFRA vs order-specific token classification (§4 step 4b)

One test: did the dependency respond at all? Deterministic map from the mined
`error_token`, verified against the actual scenario messages — not ML:

| Mined token / phrase | Class | Scenario · logger |
|---|---|---|
| `SocketTimeoutException` | INFRA | 8 · `SptClient` |
| `RFC_COMMUNICATION_FAILURE` | INFRA | 10 · `SapRfcClient` |
| `SQLTimeoutException` | INFRA | 5 · `OrderCreationService` (BM-DB) |
| `"...% below threshold"` | order-specific | 6 · `MarginCheckService` |
| `"mandatory UDF '...' missing"` | order-specific | 7 · `ValidateOrderLineUdfFields` |
| `"403"` / `"account disabled"` | order-specific | 9 · JAM |
| `"No internal SKU mapping found"` | order-specific | 4 · inbound transform |

General rule for the novel/unrecognized tail: timeout/connection/5xx-shaped
tokens → INFRA; 4xx/validation/business-rule/"not found for this order"-shaped
tokens → order-specific; **a token matching neither shape → order-specific**
(the safe default — see §4 step 4b for why; no escalation exists this
iteration, so this is a permanent per-order grouping, not a temporary one).

## 6. What goes in the embedding

- **IN:** the `message` field only, normalized with semcache's `normalize()` —
  mask `evt-…`, `\bORD-\d+\b`, `\b\d{19}\b`, `accountNumber`, other numbers/`ms`,
  timestamps; keep the stable skeleton + stable error tokens.
- **NOT IN (used as structured exact-match signals instead):** `app_name`,
  `logger`, `level`, `host`, `event_id`/`order_id`/`cart_header_id`,
  `accountNumber`, `thread`, `process_id`, `timestamp`, `log_id`.
- Rule: categorical facts compared exactly; only free-text message meaning goes
  through the vector, and only on the novel path.

## 7. Reuse from `ai_service/semcache.py` — don't rebuild

- Reuse `load_encoder()` (same model — sentence-transformers already a dep). Do
  NOT add a second model or load it in the backend.
- Reuse `normalize()` — same masking as the cache, so both features share one
  notion of "variable id".
- Reuse `diverges()` (salient-token veto) in step 5's novel path so opposite-
  meaning/similar-vocabulary lines never merge.
- **Add a new `semcache.embed(message) -> list[float] | None` function** (uses
  the already-loaded encoder + `normalize()`, returns `None` if no encoder is
  configured) and call it explicitly for every non-suppressed WARN/ERROR alert,
  both `ai` and `fallback` source. **This is NOT free** — confirmed by reading
  `semcache.py`/`graph.py`: `lookup()`/`store()` never return a vector to their
  caller, and the common exact-match cache-hit path skips `encode()` entirely
  (that's the whole point of the fast path). So attaching an embedding to every
  `ProcessedAlert` means one genuinely new `encode()` call per alert — cheap
  (local CPU, already-loaded model), but budget it as real cost, not reuse.
- The cache's *stored* vectors are unusable for clustering (keyed by template,
  in-memory, LRU-evicted) — do not read them; only reuse model + preprocessing.

## 8. WS + Teams changes

- New WS events on the existing `{type,data}` fan-out: `incident.new`,
  `incident.updated` (title, `journey_count`, `alert_count`, `last_ts`, status),
  `incident.resolved`.
- Teams: **one Adaptive Card per incident, patched in place** as it grows. Two
  requirements: (a) **suppress the per-alert Teams cards for collapsed members**
  (only the incident card posts, or Teams noise isn't actually reduced);
  (b) in-place update needs the **Power Automate / Graph** path (Incoming Webhooks
  can only post new messages) — store the Teams message id to patch it. Route the
  card to the incident's `department`, else `general`.
- Extend `channel_for(event)` for incident events; keep the fail-isolated sink
  behaviour.

## 9. Tests (extend existing suites; use v3 fixture)

- **Causal-line selection:** each of the 10 flows → correct `(subtype,
  failing_service)`; the generic `OrderProcessingService` abort is never picked;
  suppressed WARNs never picked.
- **Per-order collapse:** scenario 8's 6 alerts (4 ERROR from the 3 SPT
  timeout attempts + the generic abort, plus 2 WARN retries in between) → 1
  episode, primary = the **first** ERROR (attempt 1's `SocketTimeoutException`
  from `SptClient`) — never the generic "Order processing aborted" line, even
  though that one is also ERROR-level.
- **Spans journeys:** fire scenario 8 for 3 orders → exactly one incident,
  `journey_count=3`, correct `alert_count`, layered expansion shows 3 journeys.
- **Guard / adversarial:** inbound-DLQ (s4) vs outbound-DLQ (s10) do NOT merge;
  SPT-down vs a synthetic RSM-down do NOT merge (different `failing_service`).
- **Novel path:** an injected failure with an unknown logger/message clusters via
  embedding + veto, and does not merge with a semantically-opposite line.
- **TIMED_OUT eligibility:** a journey with no `_FAILURE_RULES` match and a real
  linked ERROR alert times out to `TIMED_OUT` but still forms/joins an incident
  via the novel path; a `TIMED_OUT` journey with no ERROR alert at all forms no
  incident.
- **Order-specific isolation:** two different orders each failing scenario 6
  (margin check) do NOT merge into one incident, no matter how many other
  orders share the same signature — each stays its own per-order episode
  permanently, this iteration (no escalation path exists — see §12).
- **Unknown-shape token default:** an injected failure whose mined token
  matches neither the INFRA nor order-specific shape defaults to
  order-specific — stays per-order, same as a confirmed order-specific
  failure would.
- **Idempotency:** redeliver alerts / re-resolve a journey → counts unchanged.
- **Lifecycle:** an incident does NOT auto-resolve on a same-`failing_service`
  SUCCESS from an unrelated order (this must NOT happen, for either INFRA or
  order-specific incidents); it resolves only via manual dashboard resolve, or
  via `INCIDENT_QUIET_TIMEOUT` as the fallback; a later matching failure after
  either kind of close opens a new incident, never a reopen.
- **LLM down:** breaker open → `fallback` alerts still carry an embedding, still
  cluster; title still generated; routes to `general`.

## 10. Open questions — resolve or ask before/while implementing

1. ~~`embedding` storage: plain JSON/array vs `pgvector`?~~ **RESOLVED: plain
   JSON/array.** Revisit only if brute-force cosine over open incidents becomes
   a real bottleneck.
2. ~~TIMED_OUT journeys — exclude from incidents or bucket separately?~~
   **RESOLVED (see §1, §4): included via the novel path, gated on having a
   linked ERROR alert.** Excluding them would mean an unrecognized failure type
   never clusters, defeating the reason embeddings were chosen at all.
3. `INCIDENT_QUIET_TIMEOUT` default (start ~ the stalled-timeout ballpark?).
4. ~~`primary_alert_id`: is a click-through primary wanted, or title-only?~~
   **RESOLVED: it's structurally required, not just a UI nicety.** It's the
   novel path's cosine-comparison target (`alerts.embedding` at
   `primary_alert_id` — see §3, §4 steps 5–6), so it has to exist and stay
   fixed regardless of whether the dashboard also uses it as a click-through.
5. `error_token` in the signature now, or keep signature = `(subtype, service)`
   and add the token later?
6. ~~Confirm the suppression list source so backend + AI service stay in sync.~~
   **RESOLVED (see §1, §5): not needed.** Causal-line selection reads the
   journey's linked `alerts`, which are already suppression-filtered by
   ai_service before they ever became an alert row.

## 11. Suggested touch list

`shared/models.py` (embedding field) · `ai_service/semcache.py` (new `embed()`
function) · `ai_service` publisher/graph (call `embed()`, attach vector, both
paths) · new `backend/incidents.py` (clustering + lifecycle sweep,
TIMED_OUT-with-ERROR eligibility) ·
`backend/consumers.py` (invoke clustering on journey completion) ·
`backend/linking.py` (reuse/extend backfill) · `backend/journeys.py` (expose
causal-line/subtype to clustering) · migration (incidents + FKs + embedding col) ·
`backend/api.py` + `backend/schemas.py` (incident reads, layered) ·
`backend/ws.py` + `backend/teams.py` (incident events, in-place card) · dashboard
(incident → orders → alerts view) · tests.

## 12. Future improvements (explicitly out of scope this iteration)

- **Escalation of a recurring order-specific failure into a systemic
  incident.** If the same order-specific signature (e.g. `MARGIN_CHECK_FAILED`)
  starts hitting many distinct orders — the classic "bad Settings push breaks
  margin checks for everyone" case — that's really a systemic-config problem
  wearing a business-rule disguise, and arguably should surface as one
  incident instead of N per-order ones. This iteration deliberately does NOT
  build that: no `INCIDENT_ESCALATION_ORDER_THRESHOLD`, no multiplicity
  tracking for order-specific signatures. Every order-specific failure stays
  scoped to its own journey, full stop. Revisit if per-order noise from a
  shared misconfiguration becomes a real problem in practice.
- **Centroid-based representative embedding.** This iteration compares the
  novel path against a fixed vector — the founding `primary_alert_id`'s
  embedding, never updated (§3, §4 steps 5–6). A running centroid (recomputed
  as an average over all joined members) could in principle match more
  accurately as an incident accumulates members, but adds real complexity
  (recompute-on-every-join, and a centroid can drift away from what any
  actual member looks like) for unclear benefit at this scale. Not built now.