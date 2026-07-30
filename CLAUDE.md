# CLAUDE.md

## What this project is

An **AI-driven log-analysis platform for a simulated order-management pipeline**
(modeled on a real system). Mock services emit
realistic logs into a mock Elasticsearch; an AI service (LangGraph) explains
WARN/ERROR logs in plain English and routes them to the right team; a core
backend assembles per-order *journeys* and feeds an IT-support dashboard
(Next.js, WebSockets) and Microsoft Teams (a deliberate deviation from the
original Slack design — see [5] Teams).

**Stack:** Python 3.11+ / FastAPI, LangGraph + Pydantic, Next.js (frontend).
Docker-compose infra: **RabbitMQ**, **PostgreSQL**, **Redis**.
LLM: **Claude via Azure AI Foundry**.

Six subsystems, chained:

```
[1] Mock Services (log emitters, ──POST logs──► [2] Mock Elasticsearch
    orchestrated by a "baton")                      (Log Collector, FastAPI :9200)
                                                        │ sliding-window poll [now-25s, now-5s]
                                                        ▼
                                                 [3] AI Service (LangGraph)
                                                     dedup (Redis SETNX)
                                                     ├─ ALL logs ────────► raw.events ─────┐
                                                     └─ WARN/ERROR → Explainer → Router    │
                                                              └────► processed.alerts ─────┤
                                                                    [4] Output RabbitMQ ◄──┘
                                                                        │ consumes both queues
                                                                        ▼
                                                 [5] Core Backend (FastAPI :8000 + PostgreSQL)
                                                     alerts + Journey Assembler
                                                        │ WebSockets / Teams webhooks
                                                        ▼
                                                 [6] Next.js IT Support Dashboard + Teams
```

Nothing here touches production — all services, hosts, and data are simulated.

## Repository layout

```
.
├── CLAUDE.md
├── docker-compose.yml            # rabbitmq (5672/15672), redis (6379), postgres (5432)
├── alembic.ini                   # DB migrations config (backend/migrations)
├── requirements.txt
├── requirements-ml.txt           # optional: semantic-cache deps (sentence-transformers + CPU torch)
├── shared/                       # cross-cutting: used by pipeline/, ai_service/, and backend/
│   ├── models.py                 # Pydantic: LogLine, Baton, ProcessedAlert
│   ├── log_client.py             # POST log lines to the collector (all services use this)
│   └── scenarios.py              # scenario definitions + step chains — single source of truth
├── pipeline/                     # the simulated order pipeline: emitters + collector + dev tooling
│   ├── services/                 # [1] one small script/app per mock service
│   │   ├── runner.py              # shared baton-consuming loop all services reuse
│   │   ├── registry.py            # (service, block) -> handler registry
│   │   ├── blocklib.py  profiles.py
│   │   ├── inbound.py  order_engine.py  spt.py  rsm.py  jam.py
│   │   ├── settings.py  checker.py  validator.py
│   │   ├── outbound_osw.py  track_trace.py
│   │   └── run_all.py             # starts every service in one command
│   ├── injector/inject.py        # starts flows (stands in for "Orders B2B / SF")
│   ├── mock_es/app.py             # [2] Log Collector, FastAPI :9200
│   ├── scripts/capture_flow.py   # dev harness: fire a scenario, dump captured logs to JSON
│   │   dump_backend.py           # dev harness: dump backend DB state to JSON
│   └── data/                     # reference fixtures (v6 = current; v2..v5 kept for history)
├── ai_service/                   # [3] :8100
│   ├── main.py  poller.py  graph.py  nodes.py  breaker.py  publisher.py  api.py
│   ├── semcache.py               # semantic cache (normalize + embed + LRU) — skips LLM on repeat log types
├── backend/                      # [5] :8000
│   ├── main.py  consumers.py  journeys.py  stitching.py  linking.py  teams.py  ws.py  db.py
│   ├── auth.py  summarizer.py    # session auth; LLM journey-summary client
│   ├── api.py  schemas.py       # read-only REST API + Pydantic response schemas
│   └── migrations/               # Alembic migrations (env.py, versions/)
├── dashboard/                    # [6] Next.js app, :3000
└── tests/
```

---

## The simulated production system (what the logs imitate)

A microservice order pipeline. One order's path in the real system:

1. Orders arrive (B2B / Salesforce) into SAP BTP — simulated by
   `pipeline/injector/inject.py`.
2. **cc-inbound-service** receives the raw order event, transforms it (maps
   vendor product ids to internal SKUs), publishes to RabbitMQ
   `order.inbound.queue`.
3. **cc-order-engine** (central orchestrator) consumes it, **creates the
   order** (persists cart header to BM DB, generates the order number), and
   publishes a **creation response** to `order.response.queue`, which inbound
   reads (→ *bridge event*, see Correlation Model).
4. cc-order-engine then enriches the order via HTTP/Feign calls, in **this
   exact order** (`ENRICH_SATELLITES` in `shared/scenarios.py` — ground truth):

   ```
   Settings → SPT → RSM → SOLR → JAM → Checker → Avalara (US only, last)
   ```

   **Settings is FIRST**: the engine reads the account's margin thresholds and
   settings before it prices anything. Then **SPT** (pricing/price lists),
   **RSM** (rebates/PVC), **SOLR** (product search / id resolution), **JAM**
   (user auth/privileges → JWT), **Checker** (margin check — can block the
   order), and finally **Avalara** (US ship-to address verification, **US orders
   only**, the last enrichment step before dispatch).

   All seven are **standalone services with their own emitters** and their own
   `serve` block. SOLR and Avalara used to be folded in elsewhere (SOLR inside
   the order engine, Avalara inside cc-validator-service); they are now
   first-class, and the validator no longer emits Avalara lines, so the ship-to
   verification appears exactly once.
5. **cc-validator-service** runs validation strategies; then RabbitMQ
   `order.outbound.queue` → **cc-outbound-osw** submits to SAP fulfilment
   (RFC) → **cc-track-trace** registers the order for tracking
   (= success terminal event).

Failed queue deliveries go to `_error` dead-letter queues
(`order.inbound.queue_error`, `order.outbound.queue_error`). The real system's
Angular UI and the ETL feeds (SAP Master Data → SPT/RSM/SOLR) are not simulated.

**In this project none of that business flow physically happens** — the mock
services only *emit the logs* the real services would produce (see [1]).

---

## Log schema (exact field names — do not deviate)

Every log line is one JSON object:

| Field | Type | Always present? | Notes |
|---|---|---|---|
| `log_id` | string (UUID) | **yes** | Unique per line. **Dedup key.** |
| `timestamp` | string | **yes** | ISO-8601 UTC, ms precision: `2026-07-14T08:00:00.432Z` |
| `app_name` | string | **yes** | e.g. `cc-inbound-service`, `cc-order-engine` |
| `level` | string | **yes** | `DEBUG` / `INFO` / `WARN` / `ERROR` |
| `logger` | string | **yes** | Java-style: `c.c.orderengine.service.OrderService` |
| `host` | string | **yes** | e.g. `CCECMEWEBT001` |
| `process_id` | string | **yes** | |
| `thread` | string | **yes** | `rabbit-listener-1`, `pool-3-thread-2`, `http-nio-8080-exec-3` |
| `eventId` | string | phase-1 field only | `evt-<uuid>` |
| `orderId` | string | phase-2 field; ALSO in creation-log *text* | `ORD-NNNN` |
| `cartHeaderId` | string | phase-2 field; ALSO in creation-log *text* | 19-digit numeric string |
| `accountNumber` | string | all phases | **Never use for correlation** — not unique per journey. |
| `message` | string | **yes** | Free text. Terminal detection AND id mining match on it — treat as an API. |

Example of a real sequence (phase 1 → creation join → bridge ack → phase 2) —
note exactly which id **fields** appear on each line, and that the join lives in
the creation-log **text**, not on any single line's fields:

```json
{"app_name":"cc-inbound-service","logger":"c.c.inbound.listener.OrderListener",
 "eventId":"evt-372656a7-...","accountNumber":"81036533",
 "message":"Received inbound order event evt-372656a7-... for account 81036533"}
   ... transform + SKU-mapping logs, eventId field only ...
{"app_name":"cc-order-engine","logger":"c.c.orderengine.service.OrderCreationService",
 "eventId":"evt-372656a7-...",
 "message":"Generated order number ORD-6001 for cart header 1840927365018240001"}   ← THE JOIN (eventId field + order ids MINED from text)
{"app_name":"cc-inbound-service","logger":"c.c.inbound.listener.ResponseListener",
 "eventId":"evt-372656a7-...",
 "message":"Received order creation response for event evt-372656a7-...: status=CREATED"}   ← BRIDGE ACK (eventId ONLY — links nothing)
{"app_name":"cc-order-engine","logger":"c.c.orderengine.service.OrderService",
 "orderId":"ORD-6001","cartHeaderId":"1840927365018240001",
 "message":"Get order by Order Number:ORD-6001"}                                ← phase 2, no eventId
```

---

## ⚠️ THE CORRELATION MODEL (most important section)

An order's logs form a **journey**. There is **no single id present on every
log of a journey** — the identifier *changes over the journey's lifetime*:

- **Phase 1 — pre-creation.** Inbound receive → transform → publish →
  order-engine consume/create: logs carry **ONLY `eventId`** as a field
  (plus `accountNumber`). No `orderId`, no `cartHeaderId` — they don't exist yet.
  **Exception (the join, see below):** the order-engine `create` block's own
  logs carry `eventId` as a field **and expose the freshly minted order ids in
  their message *text*** (`"Created cart header <19-digit>"`, `"Generated order
  number ORD-N for cart header <19-digit>"`).
- **Bridge event.** Order-engine publishes the creation response to
  `order.response.queue`; inbound logs it (logger
  `c.c.inbound.listener.ResponseListener`, message
  `"Received order creation response for event evt-...: status=CREATED"`). This
  log carries **`eventId` ONLY** — no `orderId`/`cartHeaderId`, neither as
  fields nor in its text. **It no longer links the id families** (historically
  it did; that convenience was removed deliberately). It is kept as a realistic
  ack line, not a correlation hinge.
- **Phase 2 — post-creation.** `eventId` disappears. All downstream logs
  (enrichment, checker, validator, outbound, track-trace) carry **both
  `orderId` and `cartHeaderId`** as fields.

**The join — id mining.** Because no single line carries both id families as
*fields*, the `eventId`→order-id join is recovered by **mining ids from
`log.message` text** with strict word-boundary patterns and treating them
exactly like structured-field ids (registering them in the same alias map):

| family | pattern |
|---|---|
| `eventId` | `evt-[0-9a-f-]{8,}` |
| `orderId` | `\bORD-\d+\b` |
| `cartHeaderId` | `\b\d{19}\b` |

The order-engine `create` logs (eventId field + order ids in text) close the
join **before** the bridge line even appears. Those creation-log texts are
therefore **load-bearing for correlation** — exactly like the terminal messages
are load-bearing for journey completion. Changing them requires updating the
mining patterns and their tests together. Mining is scoped to the three id
families only; `accountNumber` is never mined, and a stray 19-digit number in
unrelated prose only ever aliases the journey of the line it appears on (it
never crosses journeys, because that line already belongs to exactly one).

### Invariants (code and tests must respect these)
1. A journey = internal `journey_id` + an **alias set** of ids accumulated
   over time (from fields **and** mined text). Correlating by any single field
   is impossible.
2. **No single log line links the two id families as structured fields.** The
   join lives only in the order-engine creation logs' message text (mined). A
   single pass in timestamp order still correlates every log: those creation
   logs tie `eventId` to the order ids, and every later phase-2 log shares an
   order id already known. **Test: the honest corpus where no line links both
   families as fields still yields exactly one journey per flow.**
3. Journeys failing before creation (transform failure, creation DB failure)
   **never get order ids** — complete, valid journeys identified only by
   `eventId`. Correct behavior, not a data gap.
4. Logs of one journey can be split across polls — assembly must be
   incremental ("lazy"): a journey grows as future polls deliver more of it.
   The alias map (including mined ids) persists across polls, so a split
   between the creation logs and phase 2 still joins.

Stitching lives in **`backend/stitching.py`** (see [5]). The AI service does
NOT stitch — it processes individual logs. The reference fixture reflecting the
honest bridge is **`pipeline/data/mock-order-flows-v6.json`** — the current
reference fixture, and the one the tests read. It is **captured from the
emitters** (never hand-written), so it always reflects what the services
actually produce: the Settings-first enrichment order, `cc-solr-service` events
in every flow that reaches SOLR, and `cc-avalara-service` events in the US
success flow. v2–v5 are retained for history (v5 still shows the older
SPT-first order and has no SOLR/Avalara events).

---

## [1] Mock Services — log emitters + baton orchestration

Each service is a **standalone script that only generates its own logs and
POSTs them to the Log Collector** (`shared/log_client.py`). No business data
moves between services. What moves is a **baton** — a control message that
tells the next service "your turn to emit", carrying the flow context.

### The Baton (Pydantic, `shared/models.py`)
```json
{
  "flow_id": "internal-uuid",
  "scenario": 6,
  "steps": [["inbound","receive"],["order_engine","create"],["inbound","bridge"],
            ["order_engine","enrich_settings_call"],["settings","serve"],
            ["order_engine","enrich_settings_resp"],["order_engine","enrich_spt_call"], "..."],
  "cursor": 3,
  "ctx": {
    "eventId": "evt-...",
    "accountNumber": "81036533",
    "country": "UK",
    "user": "RFLORIA",
    "lines": [{"productId": "3652269", "sku": "SKU-GPU-A100-80GB"}],
    "orderId": null,
    "cartHeaderId": null,
    "bridge_ids": "random",
    "fail_at": null
  }
}
```

- **Transport:** RabbitMQ control queues, one per service:
  `sim.step.<service>` (e.g. `sim.step.inbound`). A service consumes a baton,
  emits the log block for `steps[cursor]`, advances `cursor`, publishes the
  baton to the next step's queue. `pipeline/services/runner.py` implements this
  loop once; each service only defines its log blocks.
- **Step chains are compiled from `shared/scenarios.py`** — the scenario
  defines the exact (service, block) sequence, including satellite
  interleaving during enrichment (OE client log → satellite server log → OE
  response log) and early termination on failures.
  - Satellite ORDER comes from `ENRICH_SATELLITES` =
    `[SETTINGS, SPT, RSM, SOLR, JAM, CHECKER]`. Nothing hardcodes it: the order
    engine derives which satellite owns the one-off orchestration preamble from
    `ENRICH_SATELLITES[0]`, so reordering the list moves the preamble with it.
  - **AVALARA is deliberately NOT in that list.** It is US-only, so the chain
    compiler appends its call→serve→resp trio conditionally on
    `ctx.country == "US"`, after CHECKER, as the last enrichment step. Its
    order-engine handlers are still registered unconditionally, or a US chain
    would dispatch to a missing block.
  - A scenario that fails **at Settings** (15/16/17) now fails at the *first*
    enrichment step, so its flow contains **no other satellite**. That is
    correct, not a truncation bug.
  - SOLR needs no special-casing per scenario: it is included automatically in
    every flow that reaches it (successes and failures that fail later — JAM,
    margin, validation, SAP) and absent from those that die earlier (transform,
    create, Settings failure, SPT-down, RSM).
- **Timing:** a service sleeps 10–110 ms (random) between its log lines, and
  the baton hop adds natural delay — so timestamps (always real `utcnow`)
  interleave realistically across concurrently running flows.
- **Id rules (this is what keeps the Correlation Model honest):**
  - `ctx.orderId`/`ctx.cartHeaderId` start null; **only order_engine's
    `create` block fills them**.
  - A service must only put into its logs the id **fields** present in `ctx`
    *at that moment* — phase-1 blocks therefore physically cannot log order-id
    fields. (The `create` block's messages still *print* the minted ids in
    their text — that text is the correlation join; see the Correlation Model.)
  - The `inbound.bridge` block logs **`eventId` only** (message ends
    `": status=CREATED"`). `ctx.bridge_ids` is now **inert** — retained on the
    baton/scenario for schema stability but ignored by the emitter.
  - Phase-2 blocks log `orderId` + `cartHeaderId` fields, **never** `eventId`.
- **Failures:** `ctx.fail_at` names the block that must emit its failure
  variant (ERROR/WARN lines, retries, DLQ message) and **stop the chain** —
  the baton is not forwarded past a fatal failure.

### Services

| Service | app_name | host | Blocks / notable logs |
|---|---|---|---|
| inbound | cc-inbound-service | CCECMETLT001 | `receive` (transform + SKU mapping, publish log), `bridge` (creation-response ack — **`eventId` only**, `": status=CREATED"`; no longer links the id families). Fail `transform`: unknown product → 3 redeliveries → `"routing message to order.inbound.queue_error"`. |
| order_engine | cc-order-engine | CCECMEWEBT001 | `create` (fills ids; creation-response publish log), `enrich` (client `--->`/`<---` Feign-style logs around each satellite), `dispatch` (publish to order.outbound.queue log). Fail `create`: BM-DB timeout ×3 → failure response (still eventId-only). |
| spt | cc-spt-service | CCECMSRVT001 | price list lookup logs. Fail `spt`: OE logs timeouts ×3 → `"Order processing aborted"`. |
| rsm | cc-rsm-service | CCECMSRVT001 | rebates / PVC rates logs. |
| solr | cc-solr-service | CCECMSRVT001 | `serve`: product search / id resolution logs. Standalone satellite (after RSM, before JAM). Success-path only — no failure variant. |
| jam | cc-jam-service | CCECMSRVT001 | auth + privileges + JWT logs. Fail `jam`: 403 account disabled → abort. |
| settings | cc-settings-service | CCECMSRVT002 | margin threshold settings; Hibernate-style SQL log. |
| checker | cc-checker-service | CCECMSRVT002 | per-line margin logs. Fail `margin`: below threshold → `"blocked by margin check"`. |
| avalara | cc-avalara-service | CCECMSRVT002 | `serve`: US ship-to address verification (**US flows only**), the last enrichment step, after Checker. Standalone satellite — the validator no longer emits these lines. Success-path only — no failure variant. |
| validator | cc-validator-service | CCECMSRVT002 | strategy logs incl. benign `"Not implemented"` WARNs (the ship-to strategy is one, for every country — the real Avalara call moved to `cc-avalara-service`). Fail `udf`: missing `costCenter` UDF → 422 → abort. |
| outbound_osw | cc-outbound-osw | CCECMEWEBT002 | SAP submission logs. Fail `sap`: RFC failure ×3 → `"moved to order.outbound.queue_error"`. |
| track_trace | cc-track-trace | CCECMEWEBT002 | `"Registered order ... for tracking"` (**success terminal**). |

### The 10 canonical scenarios (`shared/scenarios.py` — ground truth for tests)

The **Satellites** column is derived, not configured — it is what the chain
compiler produces. `SOLR` appears in every flow that reaches it; `Avalara` only
in the US flow that gets all the way to the end of enrichment.

| # | Outcome | fail_at | bridge_ids | Satellites reached |
|---|---|---|---|---|
| 1 | `SUCCESS` (UK, 3 lines) | — | both | settings → spt → rsm → **solr** → jam → checker |
| 2 | `SUCCESS` (DE via Salesforce) | — | order | settings → spt → rsm → **solr** → jam → checker |
| 3 | `SUCCESS` (US, Avalara runs) | — | cart | settings → spt → rsm → **solr** → jam → checker → **avalara** |
| 4 | `INBOUND_TRANSFORM_FAILED` | transform | — (never created) | — (dies in phase 1) |
| 5 | `ORDER_CREATION_FAILED` | create | — (never created) | — (dies in phase 1) |
| 6 | `MARGIN_CHECK_FAILED` | margin | order | settings → spt → rsm → **solr** → jam → checker |
| 7 | `VALIDATION_FAILED` | udf | both | settings → spt → rsm → **solr** → jam → checker |
| 8 | `ENRICHMENT_FAILED` (SPT down) | spt | cart | settings → spt (no solr — dies here) |
| 9 | `AUTH_FAILED` (JAM 403) | jam | order | settings → spt → rsm → **solr** → jam |
| 10 | `SAP_SUBMISSION_FAILED` | sap | both | settings → spt → rsm → **solr** → jam → checker |

Scenarios 11–17 (clustering / novel-failure cases, also in `shared/scenarios.py`)
follow the same rules: 11/12 are SPT-down (settings → spt only, and 12 is US but
never reaches Avalara — country alone is not sufficient), 13 is a margin failure
(reaches solr), 14 is SAP-down (reaches solr), and 15/16/17 fail **at Settings**,
so they reach `settings` and nothing else.

### Injector (`pipeline/injector/inject.py`)
Mints **only** `eventId` (= `evt-<uuid>`) — per the correlation model the order
ids do not exist yet — compiles the scenario's step chain into a baton, and
publishes it to `sim.step.inbound`.
`--scenario N` | `--all` (10 staggered) | `--mode continuous --interval S`.

`orderId` / `cartHeaderId` are born later, in `order_engine._mint_ids`, and
**must be unique** (`ORD-<seq>` from a counter; `cartHeaderId` = 13-digit prefix
+ 6-digit counter = **exactly 19 digits**). Uniqueness is load-bearing for
correlation, not cosmetic: two flows sharing an `orderId` get merged into one
journey, and the loser — starved of further logs — is swept as `TIMED_OUT`. Use a
counter, never `random.randint` over a small range (the original 999-value
random orderId collided within a few dozen flows). The 19-digit width is fixed by
`backend/stitching.py`'s `\b\d{19}\b` mining pattern, anchored at both ends.

---

## [2] Mock Elasticsearch — Log Collector (`pipeline/mock_es/app.py`, :9200)

FastAPI, in-memory storage. Intentionally dumb — **no journey logic here, ever**.

Storage is a **capped ring buffer** (`deque(maxlen=MOCK_ES_MAX_LOGS)`, default
200k lines): the store is unbounded by nature and the deployed simulation runs
continuously (~170k lines/day at 2 flows/30s), so an uncapped list would OOM the
container on a multi-day run. Evicting the oldest is safe because **nothing at
runtime reads old logs** — the poller only ever asks for a ~20s window and
`?id=` is debug-only. Eviction is by **insertion order, not timestamp**:
concurrent flows interleave, so the newest arrival is not necessarily the latest
timestamp, and evicting by timestamp could drop a just-arrived log that still
sits inside the poller's window.

| Endpoint | Behavior |
|---|---|
| `POST /logs` | Single log object or array. Validates `log_id` + `timestamp` (422 otherwise). Returns `{"ingested": N}`. |
| `GET /logs?from=<iso>&to=<iso>` | `from <= timestamp < to`, sorted ascending. |
| `GET /logs?id=<X>` | Logs where `eventId==X` OR `orderId==X` OR `cartHeaderId==X`, ascending. **Debug/ops tool only** — no runtime component depends on it. |
| `GET /health` | `{"status":"ok","stored":N,"capacity":MOCK_ES_MAX_LOGS,"oldest_timestamp":ISO\|null}` — `stored == capacity` means the buffer is evicting; `oldest_timestamp` is the **retention floor** (the MINIMUM timestamp held, not the left-most entry) |

`GET /logs` with **no params** is capped at `MOCK_ES_QUERY_LIMIT` (default 5000,
newest first) — an uncapped read serializes the whole store (~90 MB at the 200k
cap) on an endpoint no runtime component uses. **Windowed (`from`/`to`) and `id`
queries are never capped**: the poller's correctness depends on receiving its
entire window, so truncating one would silently drop logs.

**Retention floor and the poller watermark.** This store is in-memory while the
poller's watermark (`ai:last_to`) persists in Redis, so a collector restart — or
eviction outrunning the poller — leaves the poller asking for a window whose logs
are gone. It used to read `[]`, advance the watermark, and lose that data
silently; the journeys involved were then swept as `TIMED_OUT`, which reads like a
correlation bug rather than lost input. The poller now compares its watermark
against `oldest_timestamp` **only when a window came back empty** (so the healthy
path costs no extra request), logs a WARNING naming the gap, and skips the
watermark forward to the floor so the cycle resumes on real data. The logs in the
gap are genuinely unrecoverable — this makes the loss loud, it does not prevent
it. Expect a burst of `TIMED_OUT` journeys after any collector restart.

Only the AI service's poller reads from it at runtime.

---

## [3] AI Service (LangGraph, :8100)

### Poller (`poller.py`)
Every `POLL_INTERVAL` (default 10s) query the collector for a
**watermark-anchored** window **`[last_to, now - 5s]`** (the 5s tail is the
ingestion-lag guard). `last_to` is the previous window's `to`, persisted in
Redis (`ai:last_to`), so **consecutive windows are contiguous and no wall-clock
time is ever skipped** — a slow cycle can't drop logs. Cold start (no watermark)
falls back to `now - 25s`; after a long stall the look-back is capped at
`MAX_WINDOW_SPAN` (120s) so one catch-up read stays bounded. Overlapping
re-reads are still safe because of dedup.

```python
last_to = redis.get("ai:last_to")                          # contiguous windows
frm, to = window_from_watermark(last_to, now)              # [last_to, now-5s]
alertable = []
for log in es.range(frm, to):                              # sorted asc
    if not redis.set(f"dedup:{log['log_id']}", 1, nx=True, ex=3600):
        continue                                            # SETNX dedup
    publish("raw.events", log)                              # raw FIRST — never waits on LLM
    if log["level"] in ("WARN", "ERROR") and not suppressed(log):
        alertable.append(log)
redis.set("ai:last_to", to)                                # advance watermark
await gather(process(l) for l in alertable)                # LLM off the fetch path, bounded
```

- **Every deduped log** is published to **`raw.events`** *before* any LLM call
  (journey material for the backend must never block on the explainer/router).
- Only **WARN + ERROR** enter the LangGraph pipeline; they are processed
  **concurrently** off the fetch path, bounded by `ALERT_CONCURRENCY` (default
  4), so a burst of alerts can't serialize the poll loop.
- **Suppression list** (config, data-driven): benign WARNs that must not
  become alerts — `"Not implemented"` (validator strategies),
  `"No internal contracts found"`. They still go to `raw.events`.

> The watermark + off-critical-path processing are load-bearing: with the LLM
> live, inline per-alert calls used to block the loop long enough that the
> wall-clock window skipped logs, silently starving the journey assembler
> (journeys then `TIMED_OUT`). Do not reintroduce a `now`-anchored window or
> inline LLM calls on the fetch path.

### Semantic cache (`semcache.py`) — runs BEFORE the pipeline
The alert corpus is highly repetitive: the same handful of WARN/ERROR *types*
recur constantly, differing only by volatile ids. So before the explainer+router
runs, `process()` consults a local semantic cache; a hit reuses the cached
AI answer and **skips BOTH LLM calls**. Warm hit rate on the canonical scenarios
is ~90%+ (measured).

- **Normalization is the key trick.** The cache key is the message with volatile
  ids masked (reusing the stitcher's id shapes): `ORD-\d+`→`<ORD>`,
  `evt-…`→`<EVT>`, 19-digit→`<CART>`, **exactly-8-digit** account→`<ACC>`. So two
  same-type logs differing only by ids collapse to one key. Semantically
  meaningful tokens (retry counters `2/3`, percentages, thresholds, **7-digit
  product ids**) are **deliberately NOT masked** — masking them would merge
  alerts that must stay distinct. The account pattern is `\d{8}`, not `\d{6,}`,
  precisely so it cannot swallow a product id: `"No internal SKU mapping found
  for product 9999999"` is *about* that id, and masking it both merged distinct
  SKU-mapping alerts and made re-fill substitute the log's `accountNumber` where
  a product id belonged. (Accounts are 8 digits and product ids 7 in
  `shared/scenarios.py` — if that ever changes, this pattern changes with it.)
- **Lookup order.** normalize → exact-match on normalized text (fast path, the
  overwhelming majority of hits) → else cosine over local `all-MiniLM-L6-v2`
  embeddings (CPU, loaded once at startup, injected like `api.py`'s deps) vs
  stored vectors, taking the best only if similarity `>= SEMCACHE_THRESHOLD`
  (default 0.95). Otherwise a miss.
- **Divergence guard (cosine path only).** Cosine is blind to a *small but
  meaning-flipping* difference — `"submission succeeded"` vs `"…failed"` score
  ~0.95 on MiniLM yet must NOT share an answer. So a cosine candidate is accepted
  only if the two normalized messages also agree on their **salient tokens**
  (anything with a digit, plus configured outcome/polarity/negation words:
  failed/succeeded/passed/aborted/blocked/timeout/not/… — extend via
  `SEMCACHE_SALIENT_EXTRA`). Any salient disagreement vetoes the hit → miss. The
  exact-match path never needs the guard. **The cache always fails toward a miss:
  a false miss costs one LLM call; a false hit would serve a wrong AI-labelled
  answer. Every uncertain path (guard veto, encoder load failure, corrupt
  payload) degrades to a miss.**
- **Id re-fill on hit.** The explanation is stored NORMALIZED (ids masked); on a
  hit the CURRENT log's ids are substituted back in, so the reused explanation
  shows the right order id, never the cached one. **Every mask token
  `normalize()` can emit must be re-fillable** — `<EVT>`/`<ORD>`/`<CART>` from
  their fields (or mined from text), **`<ACC>` from `accountNumber`** — or the
  placeholder leaks verbatim into the agent-facing explanation. A token with no
  value on this log degrades to neutral prose ("the account"), never the raw
  mask. Mining applies `_MASKS` precedence, so the account pattern can never
  mine a 19-digit cart header and print it as an account.
  (Re-filling `<ACC>` is display-only and does not make `accountNumber`
  a correlation key — stitching still never consults it.)
- **Single flight — concurrent identical logs.** The store happens only AFTER both
  LLM calls return, so there is a multi-second window where an answer is being
  computed but nothing records it. The poller runs alerts **concurrently**
  (`ALERT_CONCURRENCY`) and a failure burst emits **byte-identical** lines
  milliseconds apart — so without coalescing every log in the burst misses and
  duplicates both LLM calls (measured: 4 identical logs → **8** LLM calls where 2
  suffice; on the canonical scenarios this cost ~60% of the achievable hits). The
  first caller for a normalized key is the **leader** and computes; concurrent
  callers are **followers** that await its `CachePayload` and re-fill ids from
  their OWN log (the payload is shared, never the leader's finished alert — that
  is what keeps a follower from showing the leader's order id). If the leader
  produces nothing reusable (fallback / breaker open / LLM error) followers are
  woken with `None` and run the pipeline themselves — **still failing toward a
  miss**. This is a cost optimization only: it never changes which answer a log
  gets. Per-process (there is one AI service), no new infra.
- **Provenance.** A hit keeps `source="ai"` (it *is* an AI answer, reused) so the
  backend's source-based Teams routing is untouched; it sets `cached=true` on the
  `ProcessedAlert` (the only field added to `shared/models.py`). Only `source="ai"`
  results are cached — fallbacks are never stored, so LLM-down behavior is
  unchanged.
- **Store — no new infra.** In-memory LRU (cap `SEMCACHE_MAX_ENTRIES`, default
  500) of `{normalized_text, vector, payload}`, persisted to the existing Redis
  (`ai:semcache`). Hit/miss counters (`ai:semcache:hits` / `:misses`) drive the
  hit rate on **`GET /semcache/stats`**. Cosine lookup is a linear scan — fine at
  the ≤500 cap; an ANN index would be needed only at much larger scale.
- **Deps.** Needs `sentence-transformers` + CPU `torch` (pinned in
  `requirements-ml.txt`, separate from `requirements.txt`). If the package is
  absent or the model can't load, the cache **disables itself** (deferred import)
  and the pipeline runs exactly as before — every lookup misses.

### LangGraph pipeline (`graph.py`, `nodes.py`)
```
input_queue → [semantic cache lookup] ──hit──► reuse cached answer (skip both LLM calls), source="ai" cached=true
                   │ miss (runs the pipeline unchanged, then stores an "ai" result)
                   ▼
              Explainer Node ──LLM call 1 (plain-English explanation)──► Router Node ──LLM call 2 (team)──► ProcessedAlert → processed.alerts
                   │ circuit breaker wraps the LLM calls
                   └── breaker open / LLM error ──► ProcessedAlert with explanation=null, department=null,
                                                    source="fallback"  (raw log passed straight through)
```
- **Explainer Node** — LLM call 1: plain-English explanation of the log for an
  IT-support agent (what happened, which service, likely cause).
- **Router Node** — LLM call 2: pick a `department` and a per-log `severity`
  (`critical`/`high`/`medium`/`low`), returned as one JSON object —
  `{"department": ..., "severity": ...}` and **nothing else**. Both are validated
  against their enums — an out-of-range value is an `LLMError` → fallback, never
  coerced. Severity is per-log *technical* urgency judged from the log alone (not
  business impact, not journey-level).

  There is **no `confidence`**. The router used to also return a 0–1 score; it was
  removed end to end (prompt, parsing, `ProcessedAlert`, the `alerts` column, the
  API response, the Teams card, and the dashboard). A model that volunteers a
  `confidence` key anyway is *ignored* rather than rejected — dropping an
  otherwise-valid route to fallback over an extra key would be a regression.

  **Department semantics (`_DEPARTMENT_GUIDE` + `_ROUTE_EXAMPLES` in `nodes.py`).**
  The prompt's ALLOWED list is generated from the `Department` enum, but the
  definitions and few-shot examples are hand-written — **adding a department
  updates the list automatically and silently leaves it undefined**. A test
  (`test_route_prompt_defines_every_department`) fails if the two drift.

  The load-bearing distinction is **`backend` vs `general`**:
  - `backend` = an application/integration **defect** — the service behaved wrongly.
  - `general` = **not an engineering fault**: the pipeline worked as designed and
    correctly *rejected* an order (margin below threshold, missing `costCenter`
    UDF, disabled JAM account, unmapped product). Nobody changes code. This holds
    even though the log is `ERROR`, came from a service, and says FAILED/aborted.

  Roughly half the alertable corpus is this business-rule class. Without the
  distinction the model routes on surface association (log came from a service →
  services are code → `backend`) and dumps them all on the backend team as phantom
  work — the misroute this guide exists to prevent. Note `general` is also where
  `source="fallback"` alerts land, so `#general-logs` mixes business rejections
  with unprocessed pass-throughs (distinguishable by the `AI`/`fallback` badge).
- **Fallback is a pass-through, NOT rule-based**: when the LLM is down, the
  log is sent down the pipe unexplained and unrouted (`source: "fallback"`).
  The backend routes those to the **general** Teams channel. There is no
  keyword/rule classification anywhere.
- **Circuit breaker** (`breaker.py`): 3 consecutive LLM failures → open 60s →
  half-open probe. State in Redis (`ai:breaker:state`) so it survives restarts.
  While open, skip LLM calls entirely.

### `ProcessedAlert` (Pydantic, `shared/models.py`) — contract on `processed.alerts`
```python
class Department(str, Enum):
    networking = "networking"; devops = "devops"; backend = "backend"
    database = "database"; general = "general"

class Severity(str, Enum):
    critical = "critical"; high = "high"; medium = "medium"; low = "low"

class ProcessedAlert(BaseModel):
    alert_id: str                       # uuid
    emitted_at: datetime
    log: LogLine                        # the full original log line
    explanation: str | None             # plain English; None when source="fallback"
    department: Department | None      # None when source="fallback"
    severity: Severity | None           # per-log technical severity; None when source="fallback"
    source: Literal["ai", "fallback"]
    cached: bool = False                 # True when served from the semantic cache
                                         # (still source="ai"; routing unchanged)
```

### Journey summary API (`api.py`)
`POST /summarize-journey` — body: journey meta + ordered raw logs. Returns an
LLM-written summary (services touched, where it stopped, why). Called by the
backend **on journey completion**. Same breaker; when LLM is down return a
plain template built from journey meta (`source: "fallback"`).

### LLM observability (`langsmith_stats.py`) — `GET /llm-stats`
Per-logical-model run stats (calls, p50/p99 latency, error rate, cost, tokens)
read back from LangSmith, split by the tag `llm.py` puts on each model
(`explainer` / `router` / `summary` / `chat`), plus what the semantic cache saved.
The backend forwards it (`backend/llm_stats_client.py`) so :8100 stays off the
browser; the dashboard renders it at `/ai-performance`.

**LangSmith is NOT on the read path.** A background task (`run_refresher`, started
in `main.py` beside the poller) refreshes a snapshot of all 3 windows x 4 tags on a
timer; `GET /llm-stats` is a pure snapshot read, so no user click can produce a
request to LangSmith. That is a rate-limit fix and it had to be structural: with a
TTL cache, volume was driven by clicks — 1h/24h/7d are three keys, so the first
visit to each cost 4 queries, i.e. 12 requests in a few seconds, and no TTL helps a
*first* visit. Load is now a constant function of time (12 requests per interval),
independent of how many people are watching.

Two disciplines inside the refresher:
- **Every request is spaced** by `LLM_STATS_STAGGER_SECONDS`, including across the
  window boundary — 12 back-to-back requests is the burst `/runs/stats` rejects
  even when the average rate is low. The arithmetic (12 x stagger = cycle
  duration) lives in the knob's comment in `settings.py`; keep it in step.
- **A failure never overwrites a good value.** A tag that fails this cycle keeps
  its previous number, so one 429 can't blank a card. Never a fabricated 0 — a
  value that was never read is `null`.

Three fields travel the whole chain into the UI (route → `llm_stats_client`,
including its `degraded()` shape → `lib/types.ts`) so the page can explain a null
node instead of guessing:

- **`fetched_at`** — ISO UTC, `null` until a cycle completes. Stamped at the END of
  a cycle, never the start: a cycle takes ~18s, and stamping it early would date
  the numbers from before they were gathered. Wall clock, not monotonic (it is
  persisted and displayed).
- **`langsmith_configured`** — separates "no credentials" from "nothing collected
  yet". Together with `fetched_at` these give the three distinct reasons a node is
  null: collecting / not configured / that tag's query failed. The page used to
  report all three as "not configured yet".
- **`refresh_interval_s`** — the configured period. The UI adds it to `fetched_at`
  ("next update in ~Xs") and multiplies it (`STALE_INTERVALS`) to decide the
  refresher has died, so the backend substitutes a positive default rather than
  forwarding a zero.

**The age on screen is the refresher's only health signal.** It publishes into a
snapshot, so a dead task keeps serving its last numbers — plausible, well-formed,
increasingly wrong, with no error anywhere. Past `3 x refresh_interval_s` the label
says so in words. Pure logic for all of this lives in
`dashboard/lib/aiPerformance.ts` (tested with `npm test` — Node's built-in runner,
no jest/vitest); the page polls the snapshot every 30s and has **no per-second
timer** — the data moves once a cycle, so a ticking seconds counter was false
precision.

### LLM config — Claude via Azure AI Foundry
All provider wiring in ONE module, via LangChain's chat-model abstraction:
```
AZURE_AI_FOUNDRY_ENDPOINT / AZURE_AI_FOUNDRY_API_KEY
AZURE_AI_FOUNDRY_DEPLOYMENT_EXPLAINER   # fast/cheap
AZURE_AI_FOUNDRY_DEPLOYMENT_ROUTER      # fast/cheap
AZURE_AI_FOUNDRY_DEPLOYMENT_SUMMARY     # stronger
```

---

## [4] Output RabbitMQ

Two durable queues, both published by the AI service, both consumed by the
backend:

| Queue | Payload | Purpose |
|---|---|---|
| `processed.alerts` | `ProcessedAlert` JSON | explained/routed WARN+ERROR alerts (or fallback pass-throughs) |
| `raw.events` | raw `LogLine` JSON | every deduped log — journey assembly material |

Delivery is **at-least-once** → backend consumers must be idempotent
(`alert_id` / `log_id` unique constraints).

---

## [5] Core Backend (FastAPI, :8000)

### Consumers (`consumers.py`)
- **`processed.alerts`** → dedup on `alert_id` → persist → WebSocket push
  (`alert.new`) → Teams: department channel when `source="ai"` and department
  set; **general channel** when `source="fallback"`.
- **`raw.events`** → dedup on `log_id` → feed the Journey Assembler.

### Journey Assembler (`journeys.py` + `stitching.py`)
Assembles journeys **incrementally** from the `raw.events` stream — one poll
almost never contains a full journey; later polls extend it ("lazy" assembly).

Stitching per the Correlation Model (single pass, logs processed in timestamp
order):
```python
ids = [log.eventId?, log.orderId?, log.cartHeaderId?]
jid = first id found in alias map, else new journey
register ALL ids on this log as aliases of jid
append log to journey jid
```

**Journey-over rules** (exactly these three):

| Condition | Journey outcome |
|---|---|
| Last event = track-trace `"Registered order ... for tracking"` | `SUCCESS` |
| Last event = a publish-to-`_error`-queue log (message contains `order.inbound.queue_error` / `order.outbound.queue_error`) or a fatal abort ERROR (`"Order creation failed for event"`, `"Order processing aborted"`, `"submission aborted"`, `"blocked by margin check"`) | `FAILED` (subtype from the message) |
| No new event for the journey's ids for **90s** (`STALLED_TIMEOUT`) | `TIMED_OUT` |

On journey completion: persist outcome → request LLM summary from AI service
(`POST /summarize-journey`) → WebSocket push (`journey.completed`, includes
summary) → Teams notification. While in progress, each appended chunk pushes
`journey.updated` — the dashboard's journey view fills in progressively,
possibly later than the alert that referenced it.

### PostgreSQL schema (sketch)
```
alerts(alert_id PK, emitted_at, log_id UNIQUE, level, app_name, logger, message,
       event_id, order_id, cart_header_id, account_number,
       explanation, department, severity, source, cached, journey_id FK NULL,
       is_resolved, resolved_at NULL)
journeys(journey_id PK, status, outcome NULL, first_ts, last_ts,
         event_id, order_id, cart_header_id, summary NULL)
journey_events(journey_id FK, log_id UNIQUE, ts, raw JSONB)
```

Schema changes are applied via Alembic (`backend/migrations/versions/`), not by
hand-editing tables — see "Gotchas" below.

### API
```
POST /auth/login                        # {username,password} -> sets httpOnly session cookie
                                        # (404 when PASSWORD_LOGIN_ENABLED is off)
POST /auth/logout                       # clears the cookie
GET  /auth/me                           # current user (401 if no valid session) — the frontend guard
GET  /auth/config                       # {entra_enabled, password_login} — what the login screen renders
GET  /auth/entra/login                  # 307 -> Microsoft (503 when Entra is unconfigured)
GET  /auth/entra/callback               # code -> session cookie -> 302 to DASHBOARD_URL
GET  /alerts?since=&department=&source=&cached=  # 🔒 requires session
                                        # cached=true|false is ORTHOGONAL to source:
                                        # every cached alert is source="ai", so it
                                        # narrows within AI answers, not beside them.
                                        # department/app_name/severity are MULTI-valued:
                                        # repeat the param to OR within the category
                                        # (SQL IN). Omitted / empty = no filter — an
                                        # IN () would match nothing and empty the feed.
                                        # A non-empty list excludes NULLs, so filtering
                                        # by department drops fallback alerts.
GET  /alerts/facets                     # 🔒 per-value counts for the 3 multi-selects
                                        # (severity/department/app_name). Same filter
                                        # params as /alerts, no paging. EXCLUDE-SELF:
                                        # each facet omits its OWN filter, so ticking
                                        # one value never collapses that facet's list;
                                        # the other filters still scope it. NULLs are
                                        # skipped (no filter option to count them on).
GET  /journeys?status=                  # 🔒 requires session
GET  /journeys/{id}                     # 🔒 journey + its events + summary
GET  /stats/insights                    # 🔒 aggregate counters for the insights page
                                        # journeys: total, by_status, by_outcome,
                                        # success_rate (over SUCCESS+FAILED+TIMED_OUT
                                        # only; 0 when none finished), avg_duration_seconds
                                        # alerts: total, open/resolved, by_department,
                                        # by_severity, by_level, by_source
WS   /ws                                # 🔒 alert.new | journey.updated | journey.completed
POST  /auth/login                        # {username,password} -> sets httpOnly session cookie
POST  /auth/logout                       # clears the cookie
GET   /auth/me                           # current user (401 if no valid session) — the frontend guard
GET   /alerts?since=&department=&source=&level=&app_name=&severity=&resolved=  # 🔒 requires session
PATCH /alerts/{alert_id}/resolve         # 🔒 manual triage — sets is_resolved=True, resolved_at=now()
GET   /journeys?status=                  # 🔒 requires session
GET   /journeys/{id}                     # 🔒 journey + its events + summary
WS    /ws                                # 🔒 alert.new | journey.updated | journey.completed
```

Alert filter conditions live in ONE place — `alert_filter_conditions()` in
`backend/api.py` returns the WHERE clauses as a list, which `build_alerts_query`
applies wholesale and `/alerts/facets` applies minus one clause per facet. Adding
a filter there reaches both, so the feed and its counts cannot disagree about what
a filter means (a test pins the two endpoints' query params to the same set).

Aggregation lives in **`backend/stats.py`**, split so both halves are pure and
unit-testable like `build_alerts_query`: query builders returning `Select`s with
`GROUP BY` (`alerts_by(column)` is generic over the four alert columns —
whitelisted, never interpolated), and `assemble_overview(...)` which folds the
executed rows into the response. Nullable group-by columns get an explicit bucket
(`department` → `"unassigned"`, `severity` → `"unrated"`, `outcome` → `"none"`) so
every breakdown sums back to its total instead of silently dropping nulls.

### Auth (`auth.py`, `auth_entra.py`)
Two deliberately separated layers, which is why adding Entra ID cost nothing
downstream:
- **Verification** (swappable): **Entra ID** (`auth_entra.py`) is the primary
  path — a backend-driven OAuth authorization-code flow. `authenticate()` in
  `auth.py` still matches ONE env-configured admin (`ADMIN_USERNAME` + bcrypt
  `ADMIN_PASSWORD_HASH`; dev default `admin`/`admin`), kept as the escape hatch
  for local dev and for the day the Entra client secret expires. It is gated by
  `PASSWORD_LOGIN_ENABLED` (404 when off) and MUST be `false` in a deployment —
  the default hash would otherwise be a way around SSO.
- **Session** (stable seam): `issue_token()` mints a signed JWT carried in an
  **httpOnly `oil_session` cookie**; `get_current_user` (a FastAPI dependency)
  verifies it and guards every read route (declared once at the `api.py` router
  level). BOTH login paths funnel through `issue_token` + `set_auth_cookie`, so
  they produce the same session. The `/ws` handshake authenticates with the same
  cookie (browsers can't set WS headers) — `?token=` fallback for non-browser
  clients; a bad/absent token closes with code 1008 before the client is
  registered.

**Entra flow** (`GET /auth/entra/login` → Microsoft → `GET /auth/entra/callback`):
`state`+`nonce` are stored in a signed 5-minute `oil_oauth_state` cookie
(`SameSite=lax` — the callback hop is a cross-site top-level GET, `strict` would
drop it); the callback exchanges the code via **MSAL** (synchronous, so wrapped
in `asyncio.to_thread` — the loop also runs the consumers), verifies the nonce,
mints the session, and redirects to `DASHBOARD_URL`. Every failure redirects to
`{DASHBOARD_URL}/?auth_error=<bad_state|access_denied|exchange_failed|not_configured>`
— never a raw error body, since the browser is mid-navigation on :8000. A token
response with no claims is `exchange_failed`, not `bad_state`: missing claims mean
a broken exchange, a wrong nonce means a replay. MSAL reserves
`openid`/`profile`/`offline_access`, so the code requests NO scopes and takes
identity from `preferred_username` (then `email`, then `oid`). No Graph call.
Missing `ENTRA_*` config disables Entra (503 on the login route) rather than
crashing. **Authorization is not enforced in code**: any account in the tenant
that signs in gets access — restrict it in the Azure portal (Enterprise
Application → "Assignment required" + user/group assignment), which needs no code
change. `GET /auth/config` reports `{entra_enabled, password_login}` so the login
screen knows what to render. Note logout clears only our cookie, not the Microsoft
browser session, so signing back in may not re-prompt.

Another auth method later = a new login endpoint that mints the *same* JWT via
`issue_token` and sets the *same* cookie via `set_auth_cookie` — `get_current_user`,
every guarded route, the WS check, and the frontend guard stay untouched. Don't
put auth-method specifics in the JWT payload; keep it identity + expiry.

Config: `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, `PASSWORD_LOGIN_ENABLED`,
`ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_REDIRECT_URI`,
`JWT_SECRET` (≥32 bytes in
deploy), `JWT_TTL_SECONDS` (default 8h), `AUTH_COOKIE_SECURE` (true behind TLS).
The dev-run scripts (injector, replay) POST to the collector/RabbitMQ — NOT this
API — so they are unaffected by auth. `passlib` needs `bcrypt<4.1` (pinned).

Dashboard: `lib/auth.tsx` (`AuthProvider` calls `/auth/me` on load) +
`components/auth/AuthGate.tsx` (renders `LoginScreen` when anonymous, the app
when authenticated) + a logout control in the side-nav footer. `LoginScreen`
reads `/auth/config` to decide what to offer: "Sign In With Microsoft" (a
full-page navigation to the backend, never `fetch` — OAuth needs a top-level
navigation) plus the password form only when it is enabled. Neither
`AuthProvider` nor `AuthGate` knows Entra exists — after the callback redirect
the existing `/auth/me` call just succeeds. All API/WS calls
use `credentials:"include"` so the cookie flows cross-origin (:3000 → :8000);
backend CORS sets `allow_credentials=True` and allows `POST`/`OPTIONS`.

### Teams (`teams.py`)
> **Deliberate deviation from the original spec.** The original design called for
> **Slack**; this project notifies **Microsoft Teams** instead. The routing,
> card contents, and "print to stdout when unconfigured" behaviour are otherwise
> exactly as the Slack spec described — only the transport (Teams webhooks /
> Power Automate) and the env-var names changed.

Webhook per department channel + general, Teams channels like `#devops-logs`,
... , `#general-logs`:
`TEAMS_WEBHOOK_NETWORKING`, `_DEVOPS`, `_BACKEND`, `_DATABASE`, `_GENERAL`.
Card (simple title + fields, easy to adapt between an Incoming Webhook and a
Power Automate flow): level/outcome, severity, department, service, explanation
(or "unprocessed — LLM unavailable" for `source="fallback"`), ids, `AI` vs
`fallback` badge, and a link to the dashboard journey view built from **`DASHBOARD_URL`** +
`journey_id`/`order_id`. **If a channel's webhook env var is unset, print the
card to stdout** — never crash on missing config.

Fed from the same `{"type","data"}` event stream as the WebSocket hub: routing
is a pure `channel_for(event)` — `alert.new` → its department (AI + department
set) else `general`; `journey.completed` → `general`; `journey.updated` → `None`
(ignored, would be spam). `backend/main.py` wires a fan-out `on_event` in its
lifespan that delivers each event to **both** the WS hub and Teams, isolating a
failing sink so one never stops the other or the consumers.

---

## [6] Next.js IT Support Dashboard (`dashboard/`, :3000)

Connects to backend WS + REST. Feature contract:
- Real-time alert feed with plain-English explanations.
- Department + severity per alert (no confidence score — it was removed end to
  end); **badge `AI-analyzed` vs `fallback`**, plus a
  **`Cached` badge alongside `AI-analyzed`** when `ProcessedAlert.cached` is set
  (a cache hit is the same AI answer reused — a modifier, never a replacement, so
  the two badges show together). An "Answer" filter (`all` / `cached` / `fresh`)
  maps to `?cached=`.
  (from `ProcessedAlert.source`).
- Order journey timeline view: complete path — services touched, where it
  stopped, why; per-step alert explanations where they exist; LLM journey
  summary once completed; `TIMED_OUT` flag surfaced. The journey may appear /
  fill in **later** than its alerts — the UI must handle progressive updates
  (`journey.updated`).
- **Alert filter bar** (`components/alerts/AlertFilterBar.tsx`) — department /
  source / level / service (`app_name`) / severity, shared by the Alert Feed
  and History page. Selections persist per-page in `localStorage`
  (`oil.alertFilters` / `oil.historyFilters`); `alertMatchesFilters` mirrors
  the backend query exactly, so it also gates which live `alert.new` events
  are admitted into the feed under the active filter.
- **Resolve action** — a kebab menu (`AlertActionsMenu.tsx`) on each alert card
  calls `PATCH /alerts/{id}/resolve`; a resolved alert drops out of the live
  Alert Feed immediately.
- **History page** (`/history`) — lists resolved alerts (`resolved=true`) with
  the same card/filter UI as the Alert Feed. Not real-time: no WS, just a
  re-fetch whenever the filters change.

---

## Redis keys

| Key | Type | TTL | Purpose |
|---|---|---|---|
| `dedup:{log_id}` | string | 1h | AI-service poller SETNX dedup |
| `ai:last_to` | string | — | poller watermark — `to` of the last fetched window; makes windows contiguous (see [3]). **Load-bearing, not optional.** |
| `ai:breaker:state` | hash | — | circuit breaker state |
| `ai:semcache` | string | — | semantic-cache dump (LRU entries persisted across restarts). Rebuildable — safe to drop. |
| `ai:semcache:hits` / `:misses` | string | — | semantic-cache hit/miss counters (the demo number; `GET /semcache/stats`) |
| `ai:llmstats:snapshot` | string | — | LangSmith per-model stats snapshot + its `fetched_at` (all 3 windows x 4 tags), refreshed on a timer and read by `GET /llm-stats`. Rebuildable — safe to drop; it exists so a restart doesn't blank `/ai-performance` until the first cycle lands. |

Journey state lives in Postgres — the backend owns journeys.

---

## Running everything

```bash
docker compose up -d                          # rabbitmq, redis, postgres
pip install -r requirements.txt
# Optional — enables the AI-service semantic cache (CPU torch + embeddings).
# Without it the cache disables itself and the pipeline runs unchanged.
# Windows: enable long paths first (see requirements-ml.txt) or torch fails to unpack.
pip install -r requirements-ml.txt --extra-index-url https://download.pytorch.org/whl/cpu
alembic upgrade head                          # apply DB migrations (backend/migrations)
uvicorn pipeline.mock_es.app:app --port 9200  # [2]
python -m pipeline.services.run_all           # [1] all mock services (baton consumers)
python -m ai_service.main                     # [3] poller + graph + api (:8100)
python -m backend.main                        # [5] api + consumers + ws (:8000)
cd dashboard && npm run dev                   # [6] :3000
cd dashboard && npm test                      # [6] pure-logic unit tests (node --test)
python -m pipeline.injector.inject --all      # fire the 10 scenarios
```

Env defaults: `ES_URL=http://localhost:9200`,
`REDIS_URL=redis://localhost:6379/0`,
`RABBITMQ_URL=amqp://guest:guest@localhost:5672/`,
`DATABASE_URL=postgresql://...`, `POLL_INTERVAL=10`, `WINDOW_START_OFFSET=25`,
`WINDOW_END_OFFSET=5`, `MAX_WINDOW_SPAN=120` (poller catch-up cap),
`ALERT_CONCURRENCY=4` (concurrent alert LLM calls), `STALLED_TIMEOUT=90`,
`STALLED_SWEEP_INTERVAL=15`, `MOCK_ES_MAX_LOGS=200000` (collector ring-buffer
capacity), `DASHBOARD_URL` (dashboard base for journey links),
`SEMCACHE_ENABLED=1`, `SEMCACHE_THRESHOLD=0.95` (cosine floor),
`SEMCACHE_MAX_ENTRIES=500`, `SEMCACHE_MODEL=all-MiniLM-L6-v2`, `SEMCACHE_GUARD=1`,
`SEMCACHE_SALIENT_EXTRA` (comma-sep extra guard words),
`LANGSMITH_API_KEY` + `LANGSMITH_PROJECT` (both required before `/llm-stats`
queries anything; absent = a 200 full of nulls),
`LLM_STATS_REFRESH_INTERVAL_SECONDS=60` (one cycle = 12 requests, so this alone
sets the average rate), `LLM_STATS_STAGGER_SECONDS=1.5` (pause between EVERY
request in a cycle; sized for 12 requests, not 4 — see `settings.py` for the
arithmetic), `LLM_STATS_SNAPSHOT_KEY=ai:llmstats:snapshot`,
plus Azure AI Foundry vars and the `TEAMS_WEBHOOK_*` webhooks above.
`ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` / `ENTRA_CLIENT_SECRET` (all three required
to enable Entra sign-in; absent = disabled, never a crash),
`ENTRA_REDIRECT_URI=http://localhost:8000/auth/entra/callback` (must match the
Azure app registration byte-for-byte), `PASSWORD_LOGIN_ENABLED=true` (set false in
any deployment), plus Azure AI Foundry vars and the `TEAMS_WEBHOOK_*` webhooks
above.

`LLM_STATS_CACHE_TTL_SECONDS` was **removed** with the TTL cache it belonged to —
reads no longer fetch, so there is nothing to expire.

---

## Testing

- **Correlation invariants**: phase-1 logs never contain order-id *fields*; the
  bridge ack carries `eventId` only; phase-2 logs never contain `eventId`; **no
  single line links both id families as fields**; the eventId→order-id join is
  mined from the order-engine creation logs' text; pre-creation failures produce
  eventId-only journeys; a stray 19-digit number in prose never merges journeys.
- **Cross-poll assembly**: split one flow's logs across ≥3 polls (including a
  split between the creation logs and phase 2) → exactly one journey, correct
  outcome.
- **Dedup / idempotency**: overlapping windows re-deliver logs → no duplicate
  raw.events processing, no duplicate alerts; re-delivered queue messages
  change nothing.
- **AI service**: WARN/ERROR filtering + suppression; breaker opens after 3
  failures; fallback alerts have null explanation/department,
  `source="fallback"`, and land in the general Teams channel; router output is
  always one of the 5 departments.
- **Semantic cache**: a hit reuses the cached answer WITHOUT calling the LLM
  (assert the model isn't invoked); two same-type logs with different ids hit
  (normalization); different error types miss (no false collapse); a
  retry-counter difference (`2/3` vs `3/3`) misses (meaningful tokens unmasked);
  **concurrent identical logs call the LLM ONCE** (single flight: one leader, the
  rest `cached=true`) while concurrent *distinct* types each still call it; a
  follower shows its OWN ids, never the leader's; a failed leader does not poison
  its followers (all get clean `source="fallback"`, nothing cached);
  id re-fill puts the CURRENT log's id in the reused explanation; **no mask
  token (`<ORD>`/`<ACC>`/…) ever survives re-fill, for any log** — including a
  log missing every id; a cart header is never re-filled as the account; a log
  just below `SEMCACHE_THRESHOLD` misses; the divergence guard vetoes a high-cosine
  meaning-flip (`succeeded` vs `failed`) → miss; hit/miss counters increment;
  fallbacks are never cached.
- **Journey rules**: each of the 10 scenarios ends with its expected outcome;
  killing the chain mid-flow (drop the baton) produces `TIMED_OUT` after 90s.
- **End-to-end**: `injector --all` → 10 journeys with the exact outcomes
  table, alerts visible on WS, journey completions with summaries.

## Gotchas / rules for future changes

- **Never** correlate by `accountNumber`.
- **Never** assume `orderId` exists at the start of a journey — pre-creation
  failures live and die with only `eventId`.
- The bridge ack carries `eventId` only and links nothing — do NOT reintroduce
  order-id fields on it. The eventId→order-id join comes from mining the
  order-engine creation logs' message text (`backend/stitching.py`).
- Message texts are load-bearing in **two** ways: journey terminal detection
  matches on them (`backend/journeys.py`) AND id mining extracts eventId/orderId/
  cartHeaderId from them (`backend/stitching.py`). Changing a service block's
  message — especially the order-engine `create` logs or any terminal line —
  requires updating the detection rules, the mining patterns, and their tests
  together.
- Mock services stay hollow: they emit logs and forward the baton — nothing
  else. The baton `ctx` id rules are what keep the Correlation Model honest;
  never bypass them.
- The collector is intentionally dumb; journey intelligence lives ONLY in the
  backend, alert intelligence ONLY in the AI service.
- There is **no rule-based classification** — the LLM-down path is a raw
  pass-through to the general channel. Don't reintroduce keyword routing.
- Both output queues are at-least-once: consumers must be idempotent.
- Schema changes go through Alembic (`backend/migrations/versions/`) — never
  hand-edit the tables in `backend/db.py` without a matching migration.
- The system must remain useful with the LLM completely down (breaker +
  pass-through alerts + template journey summaries). Test this path.
- The semantic cache must **fail toward a miss**, never a false hit: a false miss
  costs one LLM call, a false hit serves a wrong AI-labelled answer. Never cache a
  `source="fallback"` result. `normalize()` masks ONLY volatile ids — never mask
  retry counters/percentages/thresholds/**7-digit product ids** (that would merge
  distinct alerts). **Anything `normalize()` masks, `refill()` must be able to put
  back**, or the mask either shows up verbatim in the explanation or — worse — gets
  a wrong value substituted in its place. A
  cosine hit must pass the divergence guard. `normalize()` reuses the same id
  shapes as `backend/stitching.py`'s mining patterns — changing one means
  changing both. A hit keeps `source="ai"` (backend Teams routing depends on it);
  only `cached=true` distinguishes it.
- All LLM/provider wiring stays in one module (Azure AI Foundry today). The
  semantic-cache embedding model (sentence-transformers) is local, not a provider
  — it lives in `ai_service/semcache.py`, not `llm.py`.
- Auth methods are added, never rewired: a new one mints the same JWT via
  `issue_token` and sets the same cookie via `set_auth_cookie`. Never make a
  route, the WS handshake, or the dashboard aware of *how* someone signed in.
  The Entra `state`/`nonce` cookie must stay `SameSite=lax`, and MSAL calls must
  stay off the event loop (`asyncio.to_thread`).
- All datetimes ae UTC and timezone aware (timestamptz in Postgres,
  datetime.now(timezone.utc) in Python - never utcnow(), never naive
  datetimes). The 90s stalled journey arithmetic depends on this.