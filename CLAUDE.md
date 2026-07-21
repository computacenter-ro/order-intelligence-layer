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
├── requirements.txt
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
│   └── data/                     # reference fixtures (e.g. captured real-system log samples)
├── ai_service/                   # [3] :8100
│   ├── main.py  poller.py  graph.py  nodes.py  breaker.py  publisher.py  api.py
├── backend/                      # [5] :8000
│   ├── main.py  consumers.py  journeys.py  stitching.py  teams.py  ws.py  db.py
│   ├── api.py  schemas.py       # read-only REST API + Pydantic response schemas
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
4. cc-order-engine then enriches the order via HTTP/Feign calls:
   **SPT** (pricing/price lists), **RSM** (rebates/PVC), **SOLR** (product
   search), **Settings** (margin thresholds, SQL-backed, pushed from
   Salesforce), **JAM** (user auth/privileges → JWT), **Checker** (margin
   check — can block the order), **Avalara** (US ship-to address verification,
   US orders only).
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
| `eventId` | string | phase-1 + bridge only | `evt-<uuid>` |
| `orderId` | string | bridge (maybe) + phase 2 | `ORD-NNNN` |
| `cartHeaderId` | string | bridge (maybe) + phase 2 | 19-digit numeric string |
| `accountNumber` | string | all phases | **Never use for correlation** — not unique per journey. |
| `message` | string | **yes** | Free text. Terminal detection matches on it — treat as an API. |

Example of a real sequence (phase 1 → bridge → phase 2) — note exactly which
id fields appear on each line:

```json
{"app_name":"cc-inbound-service","logger":"c.c.inbound.listener.OrderListener",
 "eventId":"evt-372656a7-...","accountNumber":"81036533",
 "message":"Received inbound order event evt-372656a7-... for account 81036533"}
   ... transform + SKU-mapping logs, eventId only ...
{"app_name":"cc-order-engine","logger":"c.c.orderengine.service.OrderCreationService",
 "eventId":"evt-372656a7-...",
 "message":"Generated order number ORD-6001 for cart header 1840927365018240001"}
{"app_name":"cc-inbound-service","logger":"c.c.inbound.listener.ResponseListener",
 "eventId":"evt-372656a7-...","orderId":"ORD-6001","cartHeaderId":"1840927365018240001",
 "message":"Received order creation response for event evt-372656a7-...: orderNumber=ORD-6001, cartHeaderId=1840927365018240001"}   ← BRIDGE
{"app_name":"cc-order-engine","logger":"c.c.orderengine.service.OrderService",
 "orderId":"ORD-6001","cartHeaderId":"1840927365018240001",
 "message":"Get order by Order Number:ORD-6001"}                                ← phase 2, no eventId
```

---

## ⚠️ THE CORRELATION MODEL (most important section)

An order's logs form a **journey**. There is **no single id present on every
log of a journey** — the identifier *changes over the journey's lifetime*:

- **Phase 1 — pre-creation.** Inbound receive → transform → publish →
  order-engine consume/create: logs carry **ONLY `eventId`**
  (plus `accountNumber`). No `orderId`, no `cartHeaderId` — they don't exist yet.
- **Bridge event.** Order-engine publishes the creation response to
  `order.response.queue`; inbound logs it (logger
  `c.c.inbound.listener.ResponseListener`, message starts
  `"Received order creation response for event ..."`). That single log carries
  `eventId` **AND** `orderId` and/or `cartHeaderId` — **usually only ONE of
  the two (randomly); sometimes both**. It is the only log where `eventId`
  coexists with the order ids.
- **Phase 2 — post-creation.** `eventId` disappears. All downstream logs
  (enrichment, checker, validator, outbound, track-trace) carry **both
  `orderId` and `cartHeaderId`**.

### Invariants (code and tests must respect these)
1. A journey = internal `journey_id` + an **alias set** of ids accumulated
   over time. Correlating by any single field is impossible.
2. Every log that introduces a *new* id also carries an *already-known* id:
   bridge = known `eventId` + new order id(s); first phase-2 log = BOTH order
   ids, at least one known from the bridge. Single-pass stitching in timestamp
   order therefore always works, whichever id the bridge exposed.
   **Test all three bridge variants: both / orderId-only / cartHeaderId-only.**
3. Journeys failing before creation (transform failure, creation DB failure)
   **never get order ids** — complete, valid journeys identified only by
   `eventId`. Correct behavior, not a data gap.
4. Logs of one journey can be split across polls — assembly must be
   incremental ("lazy"): a journey grows as future polls deliver more of it.

Stitching lives in **`backend/stitching.py`** (see [5]). The AI service does
NOT stitch — it processes individual logs.

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
            ["order_engine","enrich"],["spt","serve"], "..."],
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
- **Timing:** a service sleeps 10–110 ms (random) between its log lines, and
  the baton hop adds natural delay — so timestamps (always real `utcnow`)
  interleave realistically across concurrently running flows.
- **Id rules (this is what keeps the Correlation Model honest):**
  - `ctx.orderId`/`ctx.cartHeaderId` start null; **only order_engine's
    `create` block fills them**.
  - A service must only put into its logs the ids present in `ctx` *at that
    moment* — phase-1 blocks therefore physically cannot log order ids.
  - The `inbound.bridge` block logs `eventId` + the ids selected by
    `bridge_ids` (`both|order|cart|random`).
  - Phase-2 blocks log `orderId` + `cartHeaderId`, **never** `eventId`.
- **Failures:** `ctx.fail_at` names the block that must emit its failure
  variant (ERROR/WARN lines, retries, DLQ message) and **stop the chain** —
  the baton is not forwarded past a fatal failure.

### Services

| Service | app_name | host | Blocks / notable logs |
|---|---|---|---|
| inbound | cc-inbound-service | CCECMETLT001 | `receive` (transform + SKU mapping, publish log), `bridge` (**the bridge log**). Fail `transform`: unknown product → 3 redeliveries → `"routing message to order.inbound.queue_error"`. |
| order_engine | cc-order-engine | CCECMEWEBT001 | `create` (fills ids; creation-response publish log), `enrich` (client `--->`/`<---` Feign-style logs around each satellite), `dispatch` (publish to order.outbound.queue log). Fail `create`: BM-DB timeout ×3 → failure response (still eventId-only). |
| spt | cc-spt-service | CCECMSRVT001 | price list lookup logs. Fail `spt`: OE logs timeouts ×3 → `"Order processing aborted"`. |
| rsm | cc-rsm-service | CCECMSRVT001 | rebates / PVC rates logs. |
| solr | cc-solr-service | CCECMSRVT001 | product search / id resolution logs. |
| jam | cc-jam-service | CCECMSRVT001 | auth + privileges + JWT logs. Fail `jam`: 403 account disabled → abort. |
| settings | cc-settings-service | CCECMSRVT002 | margin threshold settings; Hibernate-style SQL log. |
| checker | cc-checker-service | CCECMSRVT002 | per-line margin logs. Fail `margin`: below threshold → `"blocked by margin check"`. |
| avalara | cc-avalara-service | CCECMSRVT002 | US address verification (US flows only). |
| validator | cc-validator-service | CCECMSRVT002 | strategy logs incl. benign `"Not implemented"` WARNs. Fail `udf`: missing `costCenter` UDF → 422 → abort. |
| outbound_osw | cc-outbound-osw | CCECMEWEBT002 | SAP submission logs. Fail `sap`: RFC failure ×3 → `"moved to order.outbound.queue_error"`. |
| track_trace | cc-track-trace | CCECMEWEBT002 | `"Registered order ... for tracking"` (**success terminal**). |

### The 10 canonical scenarios (`shared/scenarios.py` — ground truth for tests)

| # | Outcome | fail_at | bridge_ids |
|---|---|---|---|
| 1 | `SUCCESS` (UK, 3 lines) | — | both |
| 2 | `SUCCESS` (DE via Salesforce) | — | order |
| 3 | `SUCCESS` (US, Avalara runs) | — | cart |
| 4 | `INBOUND_TRANSFORM_FAILED` | transform | — (never created) |
| 5 | `ORDER_CREATION_FAILED` | create | — (never created) |
| 6 | `MARGIN_CHECK_FAILED` | margin | order |
| 7 | `VALIDATION_FAILED` | udf | both |
| 8 | `ENRICHMENT_FAILED` (SPT down) | spt | cart |
| 9 | `AUTH_FAILED` (JAM 403) | jam | order |
| 10 | `SAP_SUBMISSION_FAILED` | sap | both |

### Injector (`pipeline/injector/inject.py`)
Creates fresh ids (`eventId` = new UUID, `orderId` = `ORD-<seq>`,
`cartHeaderId` = unique 19-digit), compiles the scenario's step chain into a
baton, publishes it to `sim.step.inbound`.
`--scenario N` | `--all` (10 staggered) | `--mode continuous --interval S`.

---

## [2] Mock Elasticsearch — Log Collector (`pipeline/mock_es/app.py`, :9200)

FastAPI, in-memory storage. Intentionally dumb — **no journey logic here, ever**.

| Endpoint | Behavior |
|---|---|
| `POST /logs` | Single log object or array. Validates `log_id` + `timestamp` (422 otherwise). Returns `{"ingested": N}`. |
| `GET /logs?from=<iso>&to=<iso>` | `from <= timestamp < to`, sorted ascending. |
| `GET /logs?id=<X>` | Logs where `eventId==X` OR `orderId==X` OR `cartHeaderId==X`, ascending. **Debug/ops tool only** — no runtime component depends on it. |
| `GET /health` | `{"status":"ok","stored":N}` |

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

### LangGraph pipeline (`graph.py`, `nodes.py`)
```
input_queue → Explainer Node ──LLM call 1 (plain-English explanation)──► Router Node ──LLM call 2 (team)──► ProcessedAlert → processed.alerts
                   │ circuit breaker wraps the LLM calls
                   └── breaker open / LLM error ──► ProcessedAlert with explanation=null, department=null,
                                                    source="fallback"  (raw log passed straight through)
```
- **Explainer Node** — LLM call 1: plain-English explanation of the log for an
  IT-support agent (what happened, which service, likely cause).
- **Router Node** — LLM call 2: pick a `department` + `confidence` (0–1).
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

class ProcessedAlert(BaseModel):
    alert_id: str                       # uuid
    emitted_at: datetime
    log: LogLine                        # the full original log line
    explanation: str | None             # plain English; None when source="fallback"
    department: Department | None      # None when source="fallback"
    confidence: float | None            # 0..1; None when source="fallback"
    source: Literal["ai", "fallback"]
```

### Journey summary API (`api.py`)
`POST /summarize-journey` — body: journey meta + ordered raw logs. Returns an
LLM-written summary (services touched, where it stopped, why). Called by the
backend **on journey completion**. Same breaker; when LLM is down return a
plain template built from journey meta (`source: "fallback"`).

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
       explanation, department, confidence, source, journey_id FK NULL)
journeys(journey_id PK, status, outcome NULL, first_ts, last_ts,
         event_id, order_id, cart_header_id, summary NULL)
journey_events(journey_id FK, log_id UNIQUE, ts, raw JSONB)
```

### API
```
GET  /alerts?since=&department=&source=
GET  /journeys?status=
GET  /journeys/{id}                     # journey + its events + summary
WS   /ws                                # alert.new | journey.updated | journey.completed
```

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
Power Automate flow): level/outcome, service, explanation (or "unprocessed —
LLM unavailable" for `source="fallback"`), ids, confidence, `AI` vs `fallback`
badge, and a link to the dashboard journey view built from **`DASHBOARD_URL`** +
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
- Department + confidence per alert; **badge `AI-analyzed` vs `fallback`**
  (from `ProcessedAlert.source`).
- Order journey timeline view: complete path — services touched, where it
  stopped, why; per-step alert explanations where they exist; LLM journey
  summary once completed; `TIMED_OUT` flag surfaced. The journey may appear /
  fill in **later** than its alerts — the UI must handle progressive updates
  (`journey.updated`).

---

## Redis keys

| Key | Type | TTL | Purpose |
|---|---|---|---|
| `dedup:{log_id}` | string | 1h | AI-service poller SETNX dedup |
| `ai:last_to` | string | — | poller watermark — `to` of the last fetched window; makes windows contiguous (see [3]). **Load-bearing, not optional.** |
| `ai:breaker:state` | hash | — | circuit breaker state |

Journey state lives in Postgres — the backend owns journeys.

---

## Running everything

```bash
docker compose up -d                          # rabbitmq, redis, postgres
pip install -r requirements.txt
uvicorn pipeline.mock_es.app:app --port 9200  # [2]
python -m pipeline.services.run_all           # [1] all mock services (baton consumers)
python -m ai_service.main                     # [3] poller + graph + api (:8100)
python -m backend.main                        # [5] api + consumers + ws (:8000)
cd dashboard && npm run dev                   # [6] :3000
python -m pipeline.injector.inject --all      # fire the 10 scenarios
```

Env defaults: `ES_URL=http://localhost:9200`,
`REDIS_URL=redis://localhost:6379/0`,
`RABBITMQ_URL=amqp://guest:guest@localhost:5672/`,
`DATABASE_URL=postgresql://...`, `POLL_INTERVAL=10`, `WINDOW_START_OFFSET=25`,
`WINDOW_END_OFFSET=5`, `MAX_WINDOW_SPAN=120` (poller catch-up cap),
`ALERT_CONCURRENCY=4` (concurrent alert LLM calls), `STALLED_TIMEOUT=90`,
`STALLED_SWEEP_INTERVAL=15`, `DASHBOARD_URL` (dashboard base for journey links),
plus Azure AI Foundry vars and the `TEAMS_WEBHOOK_*` webhooks above.

---

## Testing

- **Correlation invariants**: phase-1 logs never contain order ids; the bridge
  always has `eventId` + ≥1 order id; phase-2 logs never contain `eventId`;
  stitching succeeds for all three bridge variants; pre-creation failures
  produce eventId-only journeys.
- **Cross-poll assembly**: split one flow's logs across ≥3 polls (including a
  split right at the bridge) → exactly one journey, correct outcome.
- **Dedup / idempotency**: overlapping windows re-deliver logs → no duplicate
  raw.events processing, no duplicate alerts; re-delivered queue messages
  change nothing.
- **AI service**: WARN/ERROR filtering + suppression; breaker opens after 3
  failures; fallback alerts have null explanation/department,
  `source="fallback"`, and land in the general Teams channel; router output is
  always one of the 5 departments.
- **Journey rules**: each of the 10 scenarios ends with its expected outcome;
  killing the chain mid-flow (drop the baton) produces `TIMED_OUT` after 90s.
- **End-to-end**: `injector --all` → 10 journeys with the exact outcomes
  table, alerts visible on WS, journey completions with summaries.

## Gotchas / rules for future changes

- **Never** correlate by `accountNumber`.
- **Never** assume `orderId` exists at the start of a journey — pre-creation
  failures live and die with only `eventId`.
- The bridge may expose only one order id — both stitching directions must
  work and be tested.
- Message texts are load-bearing: journey terminal detection matches on them.
  Changing a log message in a service block requires updating the detection
  rules and tests together.
- Mock services stay hollow: they emit logs and forward the baton — nothing
  else. The baton `ctx` id rules are what keep the Correlation Model honest;
  never bypass them.
- The collector is intentionally dumb; journey intelligence lives ONLY in the
  backend, alert intelligence ONLY in the AI service.
- There is **no rule-based classification** — the LLM-down path is a raw
  pass-through to the general channel. Don't reintroduce keyword routing.
- Both output queues are at-least-once: consumers must be idempotent.
- The system must remain useful with the LLM completely down (breaker +
  pass-through alerts + template journey summaries). Test this path.
- All LLM/provider wiring stays in one module (Azure AI Foundry today).
- All datetimes ae UTC and timezone aware (timestamptz in Postgres,
  datetime.now(timezone.utc) in Python - never utcnow(), never naive
  datetimes). The 90s stalled journey arithmetic depends on this.