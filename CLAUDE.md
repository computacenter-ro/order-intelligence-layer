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
                                                     + Incident Clustering
                                                        │ WebSockets / Teams webhooks
                                                        ▼
                                                 [6] Next.js IT Support Dashboard + Teams
```

Three later additions fold into the chain rather than extending it:

- **Incident clustering** ([5], `backend/incidents.py`) collapses one order's many
  alerts into ONE incident, and merges the same *infrastructure* failure across
  different orders into one systemic incident.
- **Retrieval + assistant** (index in [3] `ai_service/ragindex.py`, asked through
  [5] `POST /chat`) answers questions over the incident history the system has
  already produced. The backend owns the DB and pushes records **up** to the AI
  service, which owns the encoder — the one reverse-direction dependency.
- **System documentation** ([3] `ai_service/docsindex.py`) is a SECOND grounding
  channel for the same `POST /chat`: per-service docs describing how the real
  pipeline works. The first two channels answer *what happened*; this one answers
  *how things work* — "what does the margin check even do, and is a margin block
  a fault?" — which no amount of incident history contains. Unlike everything
  else indexed, it comes from **files in git, not from Postgres**: a third,
  simpler path where *files own themselves*.

Nothing here touches production — all services, hosts, and data are simulated.

## Repository layout

```
.
├── CLAUDE.md
├── docker-compose.yml            # infra by default; `--profile app` runs the whole stack
├── docker-compose.prod.yml       # standalone PRODUCTION compose (Caddy ingress, GHCR images)
├── docker-compose.registry.yml   # pull-from-GHCR variant
├── Caddyfile                     # prod reverse proxy + automatic TLS (one origin)
├── Dockerfile                    # one image for every Python service (differ only by command)
├── .github/workflows/            # ci.yml (dashboard build) + build-images.yml (GHCR push)
├── alembic.ini                   # DB migrations config (backend/migrations)
├── pytest.ini                    # asyncio_mode=auto, testpaths=tests
├── requirements.txt              # includes pyyaml (doc frontmatter + service-map.yaml) — NOT optional
├── requirements-ml.txt           # optional: embedding deps (sentence-transformers + CPU torch)
│                                 #   — powers the semantic cache, incident clustering, the
│                                 #     retrieval index AND the documentation index (one model)
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
│   │   ├── settings.py  solr.py  checker.py  validator.py  avalara.py
│   │   ├── outbound_osw.py  track_trace.py
│   │   └── run_all.py             # starts every service in one command
│   ├── injector/inject.py        # starts flows (stands in for "Orders B2B / SF")
│   ├── mock_es/app.py             # [2] Log Collector, FastAPI :9200
│   ├── scripts/capture_flow.py   # dev harness: fire a scenario, dump captured logs to JSON
│   │   dump_backend.py           # dev harness: dump backend DB state to JSON
│   └── data/                     # reference fixtures (v8 = current; v2..v7 kept for history)
├── ai_service/                   # [3] :8100
│   ├── main.py  poller.py  graph.py  nodes.py  breaker.py  publisher.py  api.py
│   ├── settings.py  llm.py        # config + the ONE provider-wiring module
│   ├── semcache.py               # semantic cache (normalize + embed + LRU) — skips LLM on repeat log types
│   ├── ragindex.py               # retrieval index over incident history (RAG) — reuses semcache's encoder
│   ├── langsmith_stats.py        # per-model LLM run stats, refreshed on a timer (GET /llm-stats)
│   ├── docsindex.py              # documentation index — SECOND grounding channel, built from files
│   ├── knowledge_loader.py       # cuts knowledge/*.md into budget-bounded chunks (pure text, no ML)
│   ├── knowledge_routing.py      # question -> which service + which section kind (plain matching)
│   ├── knowledge/                # THE CORPUS (tracked): 5 service docs + answering-policy.md
│   │                             #   + service-map.yaml (mock app_name <-> doc, see_also)
│   └── scripts/eval_knowledge.py # scores docs retrieval against tests/data/knowledge_eval.yaml
├── backend/                      # [5] :8000
│   ├── main.py  consumers.py  journeys.py  stitching.py  linking.py  teams.py  ws.py  db.py
│   ├── auth.py  auth_entra.py    # session auth (password escape hatch + Entra ID SSO)
│   ├── summarizer.py             # LLM journey-summary client (calls [3])
│   ├── incidents.py              # incident clustering: signature / INFRA-vs-order / cosine+veto
│   ├── rag_client.py             # fire-and-forget push to [3] /index + ask via [3] /chat
│   ├── feedback.py               # thumbs up/down on chat answers -> ranking boost (pure)
│   ├── pagination.py             # generic keyset ("load more") pagination
│   ├── stats.py                  # insights aggregation (GET /stats/insights)
│   ├── api.py  schemas.py        # read-only REST API + Pydantic response schemas
│   ├── scripts/backfill_rag.py   # re-index every alert + completed journey (idempotent)
│   └── migrations/               # Alembic migrations (env.py, versions/)
├── dashboard/                    # [6] Next.js app, :3000 (its own CLAUDE.md -> AGENTS.md)
├── scripts/backup_db.sh
└── tests/                        # pytest; one module per subsystem
```

---

## The simulated production system (what the logs imitate)

A microservice order pipeline. The real system is **Inbound and the Order
Engine ping-ponging over RabbitMQ five times**, and the order is not persisted
until the Order Engine's *second* turn:

```
1  Orders B2B/SF → SAP BTP → Inbound            (HTTP POST /api/v1/create)
2  Inbound: write audit row, transform, SKU map → eventId born here
3  Inbound → RabbitMQ → Order Engine            [order.init]
4  Order Engine: assemble DEFAULT ORDER DATA    → NOTHING PERSISTED
5  Order Engine → RabbitMQ → Inbound            [order_data_ready]
6  Inbound → Settings                           (account settings / thresholds)
7  Inbound → JAM                                (auth + privileges → JWT)
8  Inbound → SOLR                               (catalogue-line matching)
9  Inbound → RabbitMQ → Order Engine            [order.approval]
10 Order Engine → BM DB                         ← cartHeaderId + orderId born, order INACTIVE
11 Order Engine → Track & Trace                 (mid-flow — BEFORE the checks)
12 Order Engine → SPT                           (prices)
13 Order Engine → RSM                           (rebates / PVC)
14 Order Engine → Validator                     ← auto-approval rule 1
15 Order Engine → Avalara                       ← still rule 1, US ship-to only
16 Order Engine → Checker                       ← auto-approval rule 3
17 Order Engine → RabbitMQ → Outbound OSW → SAP Fulfilment   [order.create.sap]
18 Order Engine → RabbitMQ → Inbound            [order_created]  ← closes the loop
```

Provenance (per the realignment spec's reading of the Computacenter service
docs): every hop, the two-turn Order Engine, Inbound owning Settings and JAM,
BM DB at step 10, Track & Trace *before* the checks, and the Validator →
Avalara → Checker order (auto-approval rules 1, 1 and 3, stopping at the first
failure) are **documented**. Two placements are **inferred, not documented** —
SPT/RSM at steps 12–13 (they sit before Checker only because a margin check
needs a price to judge) and SOLR at step 8 (no document mentions SOLR;
catalogue-line matching is demonstrably Inbound's job). The code comments mark
both as inferred; do not promote either to fact.

Two consequences that look like regressions and are not:

* **Settings and JAM failures are PRE-creation failures** — steps 6–7 run
  before the order exists, so those journeys die with `eventId` only and no
  order ids at all (correlation-model invariant #3, not a data gap).
* **Track & Trace is mid-flow** — its "Registered order ... for tracking" line
  appears in every journey that survives creation, including ones that later
  fail at SPT/validation/margin/SAP. It is NOT a terminal of anything; the
  SUCCESS terminal is Inbound's step-18 `order_created` close.

The two enrichment legs are ground truth in `shared/scenarios.py`:
`INBOUND_SATELLITES = [Settings, JAM, SOLR]` (pre-creation, Inbound's) and
`ENRICH_SATELLITES = [SPT, RSM, Validator, Checker]` (post-creation, the
engine's), with **Avalara inserted between Validator and Checker for US orders
only**. All satellites are standalone services with their own emitters.

Failed queue deliveries go to `<queue>_error` dead-letter queues
(`order.init_error`, `order.create.sap_error`). The real system's
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

Example of a real sequence (phase 1 → return-leg ack → creation join →
phase 2) — note exactly which id **fields** appear on each line, and that the
join lives in the creation-log **text**, not on any single line's fields:

```json
{"app_name":"cc-inbound-service","logger":"c.c.inbound.listener.OrderListener",
 "eventId":"evt-372656a7-...","accountNumber":"81036533",
 "message":"Received inbound order event evt-372656a7-... for account 81036533"}
   ... audit + transform + SKU-mapping logs, eventId field only ...
{"app_name":"cc-inbound-service","logger":"c.c.inbound.listener.OrderDataReadyListener",
 "eventId":"evt-372656a7-...",
 "message":"Received order_data_ready for event evt-372656a7-...: default order data assembled"}   ← RETURN-LEG ACK (eventId ONLY — nothing to link yet)
   ... Inbound's Settings/JAM/SOLR leg + order.approval, eventId field only ...
{"app_name":"cc-order-engine","logger":"c.c.orderengine.service.OrderCreationService",
 "eventId":"evt-372656a7-...",
 "message":"Generated order number ORD-6001 for cart header 1840927365018240001"}   ← THE JOIN (eventId field + order ids MINED from text)
{"app_name":"cc-order-engine","logger":"c.c.orderengine.service.OrderService",
 "orderId":"ORD-6001","cartHeaderId":"1840927365018240001",
 "message":"Get order by Order Number:ORD-6001"}                                ← phase 2, no eventId
```

---

## ⚠️ THE CORRELATION MODEL (most important section)

An order's logs form a **journey**. There is **no single id present on every
log of a journey** — the identifier *changes over the journey's lifetime*:

- **Phase 1 — pre-creation.** Everything up to and including the order
  engine's `create` block: Inbound receive → the engine's defaults-only first
  turn (`init`) → the `order_data_ready` return ack → **Inbound's whole
  Settings/JAM/SOLR enrichment leg** → `order.approval` → create. Logs carry
  **ONLY `eventId`** as a field (plus `accountNumber`). No `orderId`, no
  `cartHeaderId` — they don't exist yet; the pre-creation satellites are
  consulted about an order that has never been persisted.
  **Exception (the join, see below):** the order-engine `create` block's own
  logs carry `eventId` as a field **and expose the freshly minted order ids in
  their message *text*** (`"Created cart header <19-digit> with status
  INACTIVE"`, `"Generated order number ORD-N for cart header <19-digit>"`).
- **The return-leg ack.** The engine's first turn publishes
  `order_data_ready`; Inbound logs consuming it (logger
  `c.c.inbound.listener.OrderDataReadyListener`). This log carries **`eventId`
  ONLY** — there are no order ids in existence to link. (The historical
  "bridge" creation-response ack is GONE: under the five-hop flow nothing is
  published back after creation until the terminal `order_created`. The
  `ctx.bridge_ids` knob survives on the baton schema but is fully inert.)
- **Phase 2 — post-creation.** `eventId` disappears. All downstream logs
  (track-trace, SPT/RSM enrichment, validator, avalara, checker, dispatch,
  outbound, and Inbound's own `order_created` close) carry **both `orderId`
  and `cartHeaderId`** as fields.

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
join the moment the ids are born — every later phase-2 log then shares an
already-known order id. Those creation-log texts are therefore **load-bearing
for correlation** — exactly like the terminal messages are load-bearing for
journey completion. Changing them requires updating the mining patterns and
their tests together. Mining is scoped to the three id
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
3. Journeys failing before creation **never get order ids** — complete, valid
   journeys identified only by `eventId`. Under the five-hop flow that is
   transform (4) and creation-DB (5) failures **plus the Inbound-leg failures:
   JAM 403 (9) and the Settings anomalies (15/16/17)**. Correct behavior, not
   a data gap.
4. Logs of one journey can be split across polls — assembly must be
   incremental ("lazy"): a journey grows as future polls deliver more of it.
   The alias map (including mined ids) persists across polls, so a split
   between the creation logs and phase 2 still joins.

Stitching lives in **`backend/stitching.py`** (see [5]). The AI service does
NOT stitch — it processes individual logs. The reference fixture reflecting
the five-hop flow is **`pipeline/data/mock-order-flows-v8.json`** — the
current reference fixture, and the one the tests read. It is **captured from
the emitters** (never hand-written) by
**`python -m pipeline.scripts.capture_fixture --out pipeline/data/mock-order-flows-vN.json`**,
which drives every scenario's compiled chain in-process (no broker, no
collector), so it always reflects what the services actually produce: Inbound's
pre-creation Settings/JAM/SOLR leg, the mid-flow Track & Trace registration, the
Validator→Avalara→Checker rule order, and the `order_created` close. v8 adds
scenario 18's transient SPT blip; v2–v7 are retained for history (v6 still shows
the single-turn engine with its bridge ack and Track & Trace as the terminal).

When you re-capture, remember the fixture holds **randomized** values (margins,
Feign latencies) as well as minted ids — a test that compares against it must
mask both, or it will fail on the next capture without anything having changed
(`tests/test_ai_service.py::_mask_ids`).

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
  "steps": [["inbound","receive"],["order_engine","init"],
            ["inbound","enrich_settings_call"],["settings","serve"],
            ["inbound","enrich_settings_resp"],["inbound","enrich_jam_call"], "...",
            ["inbound","request_create"],["order_engine","create"],
            ["track_trace","register"],["order_engine","enrich_spt_call"], "...",
            ["order_engine","dispatch"],["outbound_osw","submit"],["inbound","close"]],
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
  interleaving on both enrichment legs (caller client log → satellite server
  log → caller response log) and early termination on failures.
  - Satellite ORDER comes from TWO lists, each the sole authority for its leg:
    `INBOUND_SATELLITES = [SETTINGS, JAM, SOLR]` (Inbound's pre-creation leg)
    and `ENRICH_SATELLITES = [SPT, RSM, VALIDATOR, CHECKER]` (the engine's
    post-creation leg). Nothing hardcodes an order: each caller derives which
    satellite owns its one-off preamble (the `order_data_ready` ack for
    Inbound, the orchestration preamble for the engine) from `LIST[0]`, so
    reordering a list moves the preamble with it. The validator's satellite
    block is `validate` (not `serve`) via `satellite_block()`.
  - **AVALARA is deliberately NOT in either list.** It is US-only, so the
    chain compiler inserts its call→serve→resp trio conditionally on
    `ctx.country == "US"`, **between VALIDATOR and CHECKER** (the documented
    rule-1 → rule-3 position). Its order-engine handlers are still registered
    unconditionally, or a US chain would dispatch to a missing block.
  - A scenario that fails **at Settings** (15/16/17) fails at the *first* stop
    on Inbound's leg — pre-creation — so its flow contains **no other
    satellite and no order ids**. That is correct, not a truncation bug.
  - SOLR needs no special-casing per scenario: as the last stop on Inbound's
    leg it is included automatically in every flow that survives JAM —
    including ones that die later at create (5) or SPT (8/11/12) — and absent
    from those that die earlier (transform, Settings failure, JAM).
- **Timing:** a service sleeps 10–110 ms (random) between its log lines, and
  the baton hop adds natural delay — so timestamps (always real `utcnow`)
  interleave realistically across concurrently running flows.
- **Id rules (this is what keeps the Correlation Model honest):**
  - `ctx.orderId`/`ctx.cartHeaderId` start null; **only order_engine's
    `create` block fills them**.
  - A service must only put into its logs the id **fields** present in `ctx`
    *at that moment* — phase-1 blocks (steps 1–9 of the flow, i.e. everything
    through `create`, including Inbound's whole Settings/JAM/SOLR leg)
    therefore physically cannot log order-id fields. (The `create` block's
    messages still *print* the minted ids in their text — that text is the
    correlation join; see the Correlation Model.)
  - The `order_data_ready` ack (start of Inbound's leg) logs **`eventId`
    only**. There is no creation-response bridge block anymore;
    `ctx.bridge_ids` is **inert** — retained on the baton/scenario for schema
    stability but read by nothing.
  - Phase-2 blocks log `orderId` + `cartHeaderId` fields, **never** `eventId`.
    That includes Inbound's own `close` block — the success terminal.
- **Failures:** `ctx.fail_at` names the block that must emit its failure
  variant (ERROR/WARN lines, retries, DLQ message) and **stop the chain** —
  the baton is not forwarded past a fatal failure.

### Services

| Service | app_name | host | Blocks / notable logs |
|---|---|---|---|
| inbound | cc-inbound-service | CCECMETLT001 | `receive` (audit row + transform + SKU mapping, publish `order.init`), `enrich_{settings,jam,solr}_call/_resp` (the pre-creation leg; the first call block emits the **`order_data_ready` ack** — `eventId` only), `request_create` (publish `order.approval`), `close` (**the SUCCESS terminal**: `"Received order_created for order ORD-N: order processing complete"`, phase 2). Fail `transform`: unknown product → 3 redeliveries → `"routing message to order.init_error"`. |
| order_engine | cc-order-engine | CCECMEWEBT001 | `init` (FIRST turn: default order data, persists **nothing**, publishes `order_data_ready`), `create` (SECOND turn: persists to BM DB — order INACTIVE — and fills ids; its message texts are the correlation join), `enrich_{spt,rsm,validator,avalara,checker}_call/_resp` (client `--->`/`<---` logs; checker and validator have no client lines), `dispatch` (publish `order.create.sap`). Fail `create`: BM-DB timeout ×3 → `"Order creation failed for event ..."` (still eventId-only; nothing published back). |
| spt | cc-spt-service | CCECMSRVT001 | price list lookup logs. Fail `spt`: OE logs timeouts ×3 → `"Order processing aborted"`. |
| rsm | cc-rsm-service | CCECMSRVT001 | rebates / PVC rates logs. |
| solr | cc-solr-service | CCECMSRVT001 | `serve`: catalogue-line matching, **last stop on Inbound's pre-creation leg** (placement inferred — no doc mentions SOLR). Phase-1 logs (eventId only). Success-path only — no failure variant. |
| jam | cc-jam-service | CCECMSRVT001 | auth + privileges logs, **on Inbound's pre-creation leg**. Fail `jam`: 403 account disabled → Inbound-identity abort (`"not authorized (403 from JAM); submission aborted"`) — a PRE-creation failure, eventId-only journey. |
| settings | cc-settings-service | CCECMSRVT002 | margin threshold settings; Hibernate-style SQL log. **First stop on Inbound's pre-creation leg.** Failure variants (15/16/17) are Inbound-identity `SettingsClient` ERRORs — deliberately unrecognized (the anomaly / embedding path), pre-creation. |
| checker | cc-checker-service | CCECMSRVT002 | per-line margin logs — auto-approval **rule 3**, last of the engine's checks. Fail `margin`: below threshold → `"blocked by margin check"`. |
| avalara | cc-avalara-service | CCECMSRVT002 | `serve`: US ship-to address verification (**US flows only**), **between Validator and Checker** (still rule 1). Success-path only — no failure variant. |
| validator | cc-validator-service | CCECMSRVT002 | `validate`: strategy logs incl. benign `"Not implemented"` WARNs — auto-approval **rule 1**, first of the engine's checks. Fail `udf`: missing `costCenter` UDF → 422 → abort. |
| outbound_osw | cc-outbound-osw | CCECMEWEBT002 | SAP submission logs. Fail `sap`: RFC failure ×3 → `"moved to order.create.sap_error"`. |
| track_trace | cc-track-trace | CCECMEWEBT002 | `register`: `"Registered order ... for tracking"` — **mid-flow**, immediately after creation, before the checks. **NOT a terminal.** |

### The scenarios (`shared/scenarios.py` — ground truth for tests)

**1–10 are the canonical set** — one per distinct outcome. Every test that means
"the corpus" means these ten.

The **Satellites** column is derived, not configured — it is what the chain
compiler produces. The `‖` marks the creation boundary: everything left of it
is Inbound's pre-creation leg (eventId-only logs), everything right of it the
engine's post-creation leg. `bridge_ids` no longer appears — it is inert.
Every flow that crosses `‖` also registers with Track & Trace right after
creation, even ones that fail later.

Two knobs drive abnormal behaviour, and they are **not interchangeable**:
`fail_at` means "emit the failure variant AND stop" — `compile_steps` truncates
the chain at it — while **`flaky_at`** (scenario 18) means "fail once, then
recover", so the compiler deliberately **ignores** it and the flow runs the full
chain. Expressing recovery through `fail_at` would truncate the chain and the
journey could never complete; a test pins that the compiler never reads
`flaky_at`.

| # | Outcome | fail_at | Satellites reached |
|---|---|---|---|
| 1 | `SUCCESS` (UK, 3 lines) | — | settings → jam → solr ‖ spt → rsm → validator → checker |
| 2 | `SUCCESS` (DE via Salesforce) | — | settings → jam → solr ‖ spt → rsm → validator → checker |
| 3 | `SUCCESS` (US, Avalara runs) | — | settings → jam → solr ‖ spt → rsm → validator → **avalara** → checker |
| 4 | `INBOUND_TRANSFORM_FAILED` | transform | — (dies at receive) |
| 5 | `ORDER_CREATION_FAILED` | create | settings → jam → solr (dies AT create — still eventId-only) |
| 6 | `MARGIN_CHECK_FAILED` | margin | settings → jam → solr ‖ spt → rsm → validator → checker |
| 7 | `VALIDATION_FAILED` | udf | settings → jam → solr ‖ spt → rsm → validator |
| 8 | `ENRICHMENT_FAILED` (SPT down) | spt | settings → jam → solr ‖ spt |
| 9 | `AUTH_FAILED` (JAM 403) | jam | settings → jam (PRE-creation — no order ids) |
| 10 | `SAP_SUBMISSION_FAILED` | sap | settings → jam → solr ‖ spt → rsm → validator → checker |
| 18 | `SUCCESS` (SPT blips, recovers) | — (`flaky_at=spt`) | settings → jam → solr ‖ spt → rsm → validator → checker |

Scenarios 11–17 (clustering / novel-failure cases, also in `shared/scenarios.py`)
follow the same rules: 11/12 are SPT-down (like 8 — and 12 is US but never
reaches Avalara: country alone is not sufficient), 13 is a margin failure
(like 6), 14 is SAP-down (like 10), and 15/16/17 fail **at Settings** — the
first stop on Inbound's leg — so they reach `settings` and nothing else, and
die **pre-creation** with eventId only.

**11–17 exist to exercise incident clustering** (see [5] Incident Clustering) —
they add no new failure *modes*, they put SEVERAL orders through the same one so
you can watch clustering merge or refuse to:

| # | Outcome | fail_at | Clustering point |
|---|---|---|---|
| 11, 12 | `ENRICHMENT_FAILED` (DE, US) | spt | 8 + 11 + 12 → **ONE** incident, `journey_count=3` (INFRA merges across orders) |
| 13 | `MARGIN_CHECK_FAILED` (other account) | margin | 6 + 13 → **TWO** incidents (order-specific never merges) |
| 14 | `SAP_SUBMISSION_FAILED` (DE) | sap | 10 + 14 → **ONE** incident |
| 15, 16 | `UNRECOGNIZED_FAILURE` | settings | novel path: no `_FAILURE_RULES` match → **embeddings**. 15 + 16 mean the same thing worded differently → **ONE** incident |
| 17 | `UNRECOGNIZED_FAILURE` | settings_rejected | same satellite as 15/16 but a different cause (503) → the **divergence guard vetoes** the cosine match → its OWN incident |

15–17 are the only scenarios that **require an encoder** (`requirements-ml.txt`);
11–14 resolve to recognized subtypes and cluster by signature hash alone. Note
`shared/scenarios.py` documents an outstanding TODO on 16: `fail_at="settings"`
currently emits flow 15's exact message, so until `pipeline/services/settings.py`
gains a differently-worded variant, 15 and 16 are byte-identical rather than
"same meaning, different wording".

**18 is the TRANSIENT failure** — the only scenario that fails and recovers.
1–17 either succeed cleanly or fail terminally (every retry loop in
`pipeline/services/` exhausts), which models only the terminal tail of the real
system: `ai_service/knowledge/inbound-order.md` §7.2 documents Feign clients
retrying 4× with backoff on 5xx, so a downstream blip recovering is the NORMAL
case. SPT times out once, the retry succeeds, and the flow completes as
`SUCCESS`. Three things make that work, all load-bearing:

* the recovery variant emits the SAME timeout ERROR and retry WARN as the outage
  (a blip and an outage are indistinguishable until the retry lands) but **never**
  the fatal `"Order processing aborted"` line — the only line in SPT's failure
  path `_FAILURE_RULES` matches;
* the recovery marker comes from the **same logger** as the timeout
  (`SptClient`), which is what `backend/journeys.py`'s `unrecovered_errors`
  reads;
* the journey therefore carries a real ERROR **and** resolves SUCCESS — which is
  the point: an ERROR alone must not condemn a journey.

This is the first flow to attach WARN/ERROR **alerts to a SUCCESS journey**. On
the dashboard it reads as an order that succeeded while showing red alerts —
which is honest: the blip really happened, and a service flapping is worth
seeing. Those alerts DO cluster, into a `TRANSIENT_FAILURE` incident of their own
(see [5] Incident Clustering, Eligibility) — separate from the outage incident
for the same service, so the failure incident's blast radius stays truthful while
the recovered alerts remain bulk-resolvable.

### Injector (`pipeline/injector/inject.py`)
Mints **only** `eventId` (= `evt-<uuid>`) — per the correlation model the order
ids do not exist yet — compiles the scenario's step chain into a baton, and
publishes it to `sim.step.inbound`.
`--scenario N` | `--all` (every scenario in `SCENARIOS`, staggered) |
`--mode continuous --interval S`. The `--scenario` help text and validation are
generated from `SCENARIOS`, so they cannot drift from the table above.

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
  `ProcessedAlert`. Only `source="ai"` results are cached — fallbacks are never
  stored, so LLM-down behavior is unchanged.
- **The encoder is shared, not private.** `semcache.Encoder` / `load_encoder` /
  `cosine` / `as_floats` are imported by `ai_service/ragindex.py` (the retrieval
  index) and the masked-message vector is attached to every alert as
  `ProcessedAlert.embedding` for the backend's clustering. One model, loaded once
  at startup, three consumers — which is what keeps "no new ML dependency" true.
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
    embedding: list[float] | None = None # masked-message vector, for the backend's
                                         # incident clustering. None when no encoder
                                         # is configured — clustering's novel path
                                         # then simply has nothing to compare.
```

### Retrieval index (`ragindex.py`) — semantic search over incident history
Same embedding stack as the semantic cache, **opposite goal**. The thresholds
differ by design and must not be unified:

| | semcache | ragindex |
|---|---|---|
| question | "have I answered THIS log before?" | "which past incidents relate to this question?" |
| keyed by | normalized message text | caller-supplied record id |
| threshold | `0.95` — near-identical only | `0.30` (`RAGINDEX_MIN_SCORE`) — a loose recall **floor**, not a precision gate |
| a wrong answer | serves a wrong AI answer | shows a less relevant source |
| size | ~500 log **types** | ~5000 incident **records** |

- **Who owns what.** The **backend owns the DB** and decides what is worth
  indexing (a persisted alert, a completed journey) and filtering on; the **AI
  service owns the encoder**. So the backend POSTs text + free-form `metadata` to
  `POST /index` and no ML dependency is added to [5]. `metadata` is deliberately
  untyped so a new filter key needs no AI-service change.
- **Upsert by record id**, which is what makes `backend/scripts/backfill_rag.py`
  idempotent. Eviction is **insertion-order (oldest first), NOT LRU** — unlike a
  cache, a rarely-retrieved incident isn't less valuable than a popular one; age is
  the honest proxy. Re-indexing an existing id keeps its original position, so a
  backfill re-run cannot reshuffle age.
- **Store:** in-memory dict persisted to the existing Redis (`ai:ragindex`), cap
  `RAGINDEX_MAX_ENTRIES`. Linear cosine scan — fine at 5000; ANN only at much
  larger scale. If the encoder can't load, the index **disables itself** and
  retrieval returns `[]` (the semcache self-disabling pattern).
- **Feedback-aware ranking** (see [5] `feedback.py`): `final = cosine * (1-w) +
  boost * w`, `w = RAGINDEX_FEEDBACK_WEIGHT` (0.15). Three load-bearing
  properties: the `min_score` floor is applied to **raw cosine, never the blend**
  (feedback re-orders what already matched — no amount of likes can lift an
  irrelevant record over the relevance floor); a record with no votes counts as
  **NEUTRAL 0.5, not zero**; and `w=0` is an exact no-op. Both `score` and
  `final_score` come back so the effect is inspectable.

### Documentation index (`docsindex.py` + `knowledge_loader.py` + `knowledge_routing.py`)
The second grounding channel: per-service docs in **`ai_service/knowledge/`**
(5 service docs + `answering-policy.md` + `service-map.yaml`). Same encoder again —
one model, now four consumers.

**Why a separate index and not a new `kind` in `ragindex`** — four reasons, the
first decisive:
1. `ragindex` evicts **oldest-first**. Docs load once at startup, so they are
   permanently the oldest entries and the continuous alert stream would silently
   evict them within days — no error, just a chatbot that stops knowing what JAM is.
2. One shared `k` makes docs and alerts compete: "what does the checker do?" loses
   its own documentation to five checker *failures*. Separate budgets **guarantee**
   a mix instead of hoping for one.
3. `RAGINDEX_MIN_SCORE` is tuned for incident history.
4. Feedback boosts would let a downvote on a badly-worded answer demote correct
   reference material — so this index is queried with **no feedback blend at all**.

**Chunking — a chunk is a section, bounded by a token budget.** Two constraints,
and the second is the one that matters:
1. `all-MiniLM-L6-v2` truncates at ~256 word-piece tokens. 13 of jam-ws's 21
   sections exceed that; §11 is ~1114 tokens, so three quarters of it would never
   be embedded — silently, with every structural test still passing.
2. **One chunk becomes one vector, i.e. one meaning.** §11 is 17 unrelated
   warnings; averaged together they match every jam-ws question weakly and none
   well. **A bigger encoder fixes (1) and leaves (2) untouched** — which is why the
   answer is chunking, not a model swap (a swap would also invalidate every vector
   already in `alerts.embedding` and de-tune all three cosine thresholds).

So sections split at their own natural repeated unit — table row, list item, bold
sub-block, paragraph — never mid-sentence, packed up to `CHUNK_BUDGET_CHARS`. A
table row is always re-issued **with its header** (a row torn from its header means
nothing), and every piece is prefixed `[service · heading]` so a fragment carries
its identity into any prompt. Each chunk also keeps its **whole parent section**,
so retrieval can match the precise piece and still hand the LLM the surrounding
context — the useful half of "summarise each section", with no LLM and no loss of
exact wording. Current corpus: **274 chunks**, avg ~712 chars.

**Chunk ids are content-derived, never positional** —
`jam-ws#blind-spots-and-traps--role-matching-is-exact`, not `jam-ws#11-4`. Ordinals
shift the moment anyone inserts a section, silently re-pointing every citation
already shown to a user and invalidating the evaluation set.

**Section kinds come from heading TEXT, never the number.** The five docs word the
same section differently ("The rules that stop **an order**" vs "…stop
**something**") and the numbering will be revised, so `kind_for()` keyword-matches
the heading and a nested section **inherits its parent's verdict** — which is what
keeps §7.1/7.2/7.3 out of the index when their own headings match no rule. §7 (log
anatomy — engineer-level, and a logback format the simulation never emits) and §13
(provenance — about the document) are excluded outright. An **unrecognised heading
is indexed unlabelled, never dropped**.

**Retrieval is three steps** (`knowledge_routing.py`), cheapest first:
1. exact match on a pasted log message — **not built yet**; the highest-value
   support action and it needs no AI at all.
2. **narrow by service and question kind**, by plain string matching. Service
   aliases come from each doc's own frontmatter (single source of truth) plus
   `service-map.yaml`, which adds the mock `cc-*` app_names — so a question scoped
   to a `cc-jam-service` alert narrows to `jam-ws` without the user naming it.
3. cosine over what survives.

Measured on `tests/data/knowledge_eval.yaml` (17 questions, 274 chunks): similarity
alone **top-1 29% / top-3 53%**; adding the question-kind filter **41% / 82%**. The
narrowing is not optional — unfiltered, one chunk about the three spellings of the
word "JAM" won three unrelated questions.

- **Routing is a fast path, not a gate.** No service named, or an unrecognised
  phrasing, means search wider — never refuse (you lose precision, never the answer).
- **`kind_mode="soft"`** ranks kind-matched chunks first but still fills leftover
  slots, so a mis-detected kind costs ranking, not the answer. Question phrasing is
  genuinely ambiguous in a way a service name is not.
- **A named-but-undocumented service does NOT fall through to the wide search.**
  That is knowledge, not ignorance. `see_also` in `service-map.yaml` points at the
  doc covering it second-hand (`cc-checker-service` → `order-engine`, and
  `folded_in` components inherit their `emitted_by` service's doc); with no
  `see_also` the docs channel returns `[]`, which is what makes "I have no
  documentation for SPT" deterministic rather than four unrelated RSM sections.
- **No persistence, deliberately.** Rebuilt from the files in seconds, so a Redis
  copy could only drift. `GET /docs/stats` reports size/kinds/why-it-is-off;
  `POST /docs/reload` re-reads without a restart, and a **failed reload keeps the
  previous index** rather than emptying it.
- **Self-disabling** like semcache/ragindex: no encoder, missing folder or an
  unreadable corpus all leave an empty index and an assistant that answers from
  incident history alone — never a crash at startup.
- **`answering-policy.md` is the system prompt, never an indexed chunk.** It is the
  only corpus file with **no `yaml` frontmatter block**, and that absence — not a
  hardcoded filename — is the loader's test for "not a service doc".

**Eight services are still undocumented** (SPT, Settings, Checker, SOLR, Avalara,
Track & Trace, Outbound OSW, SAP/Salesforce); `service-map.yaml` records them so the
"no documentation" answer is a lookup rather than a guess.

### Chat / summary API (`api.py`)
`POST /summarize-journey` — body: journey meta + ordered raw logs. Returns an
LLM-written summary (services touched, where it stopped, why) plus a
`suggested_label` used as an incident title on the novel path. Called by the
backend **on journey completion**. Same breaker; when LLM is down return a
plain template built from journey meta (`source: "fallback"`).

`POST /chat` — retrieve from **both** grounding channels (incident history via
`ragindex`, documentation via `docsindex`), then compose an answer grounded in
what came back, under the same breaker. Degradation is layered so the endpoint is
always useful, and the `sources` are **identical in every case** — only the prose
differs, so a UI never branches on mode to render citations:

| situation | `mode` |
|---|---|
| any context retrieved + LLM ok | `"ai"` — a grounded narrative |
| breaker open / LLM error | `"retrieval-only"` — deterministic template listing what was found |
| nothing retrieved anywhere | `"retrieval-only"` — says so plainly, never invents a source |

`self_grounded` (bool, default false) is the caller asserting **"grounding
material is in `query` itself"** — the backend sets it when it prepended a scoped
record's text, which is read LIVE from Postgres and is more authoritative than
anything indexed. Without it, "retrieval found nothing" and "there is nothing to
answer from" are the same fact, which was true only while the index was the sole
channel. The failure it fixes: click a NOVEL failure (nothing similar is indexed —
exactly when you need help), ask "what does this mean?", and get *"No related
incidents found — run the backfill"* while the alert's full text sits in that very
request. The original guard still holds for the case it was written for: a bare
question that matched nothing composes nothing.

**Record ids never appear in the answer prose.** They identify rows the reader
cannot look up by id, and the UI lists the sources beside the answer anyway. The
prompt forbids bracketed citations, and `clean_answer_citations()` is the
deterministic backstop ("do not do X" is a conditional instruction a small
deployment follows unreliably — the same reason `coverage` became a computed
field). Doc ids are SUBSTITUTED with "the official documentation" so the sentence
survives; alert/journey ids are DELETED, with the orphaned "Evidence:" lead-in
removed as a unit. **Business identifiers (`ORD-6426`, `evt-…`, cart header ids)
are never touched** — only ids of retrieved records, and only inside brackets.

`coverage` (`shown` / `limit` / `truncated`) is **computed by the server, never
generated**, and describes the **incident channel only**: "truncated" answers "is
there more history I did not see?", a real risk on an ever-growing record set,
whereas the docs corpus is small, fixed and authored. Top-k has no notion of
coverage: several alerts about one incident can crowd out a second distinct
incident that also matched, and an answer built from that sample reads as if it
described the whole history. Two prompt-based attempts at self-caveating were
followed about half the time in each direction; a field is deterministic and costs
no tokens. `truncated` is the one a caller should act on.

`GET /semcache/stats`, `GET /ragindex/stats` and `GET /docs/stats` expose cache
hit rate and index sizes. `POST /docs/reload` rebuilds the documentation index
from the folder without a restart.

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
All provider wiring in ONE module (`llm.py`), via LangChain's chat-model
abstraction:
```
AZURE_AI_FOUNDRY_ENDPOINT / AZURE_AI_FOUNDRY_API_KEY / AZURE_AI_FOUNDRY_API_VERSION
AZURE_AI_FOUNDRY_DEPLOYMENT_EXPLAINER   # fast/cheap
AZURE_AI_FOUNDRY_DEPLOYMENT_ROUTER      # fast/cheap
AZURE_AI_FOUNDRY_DEPLOYMENT_SUMMARY     # stronger
AZURE_AI_FOUNDRY_DEPLOYMENT_CHAT        # stronger — grounded /chat composition
```
The embedding model (sentence-transformers) is **local, not a provider** — it
lives in `semcache.py`, never in `llm.py`.

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
- **`processed.alerts`** → dedup on `alert_id` → persist → link to its journey
  (`linking.py`, which also backfills the alert's `incident_id` if that journey is
  already clustered) → WebSocket push (`alert.new`) → Teams: department channel
  when `source="ai"` and department set, **general channel** when
  `source="fallback"` → push to the retrieval index (fire-and-forget).
- **`raw.events`** → dedup on `log_id` → feed the Journey Assembler.
- Everything after "persist" is gated on the **same insert rowcount**, so a
  redelivered duplicate does no extra work — the at-least-once contract, applied
  to the broadcast and the index push as well as the row.
- Two periodic sweeps run alongside (`STALLED_SWEEP_INTERVAL`): the stalled-journey
  sweep, and **`retry_unclustered_completions`** (see Incident Clustering below).

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

**Journey-over rules.** Two are message-driven (pure, in `detect_terminal` /
`classify_failure`) and one is time-driven; the fourth row is what a stalled
journey that *did* log a real ERROR resolves to:

| Condition | Journey outcome |
|---|---|
| Last event = Inbound's `order_created` close (`"Received order_created for order ... : order processing complete"` — BOTH markers). **NOT** Track & Trace: `"Registered order ... for tracking"` is mid-flow and terminates nothing. | `SUCCESS` |
| A dead-letter routing marker (message contains `order.init_error` / `order.create.sap_error`, or the legacy `order.inbound.queue_error`/`.dlq` / `order.outbound.queue_error`/`.dlq` spellings) or a fatal abort ERROR (`"Order creation failed for event"`, `"Order processing aborted"`, `"submission aborted"`, `"blocked by margin check"`, JAM `"not authorized"` 403, `"Max redelivery attempts reached"`) | `FAILED` (subtype from the message, via `_FAILURE_RULES`) |
| Stalled (no new event for the journey's ids for **90s**, `STALLED_TIMEOUT`) **and** an **UNRECOVERED** ERROR was logged that no `_FAILURE_RULES` marker matched | `FAILED` / **`UNRECOGNIZED_FAILURE`** |
| Stalled with **no** ERROR signal at all, **or only recovered ones** | `TIMED_OUT` |

**"Unrecovered" is the load-bearing word** (`unrecovered_errors` in
`backend/journeys.py`). An ERROR counts as *recovered* when a LATER log in the
same journey comes from the same source — same `app_name` **and** same `logger`
— at INFO/DEBUG: the client that just failed went on to log healthy activity,
which is exactly what a Feign retry succeeding looks like (scenario 18). The
condition used to be "any ERROR anywhere", which labelled a journey with a
recovered blip `FAILED` and named that blip as its apparent cause — sending it
down clustering's embedding path, where it could merge with unrelated incidents.
Both halves of the pair matter: `app_name` alone would let any later
`cc-order-engine` INFO clear a genuine outage, and only lines strictly *after*
the ERROR count, or an outage's own healthy preamble would retro-clear it.

`UNRECOGNIZED_FAILURE` is deliberately distinct from `TIMED_OUT`: "something broke
and we don't recognize it" is a different fact from "it went quiet". It is the
trigger for incident clustering's embedding path (below) — and it is what the
Settings failures (15/16/17) resolve to.

On journey completion: persist outcome → request LLM summary from AI service
(`POST /summarize-journey`, whose `suggested_label` is stored on
`journeys.suggested_failure_label` for use as a novel incident's title) → cluster
into an incident → WebSocket push (`journey.completed`, includes summary) → Teams
notification → push to the retrieval index. While in progress, each appended chunk
pushes `journey.updated` — the dashboard's journey view fills in progressively,
possibly later than the alert that referenced it.

### Incident Clustering (`incidents.py`)
An order that fails produces many alerts, and one outage produces many failing
orders. This collapses both: **one order's alerts → one incident**, and for
**infrastructure-class failures only**, the same failure across different orders →
**one systemic incident**. Split like `journeys.py`: pure decision functions
(causal-line pick, signature, classification, cosine + veto) with a thin
DB-touching layer over them.

1. **Causal line** — first `ERROR` among the journey's alerts, else first `WARN`.
   Every canonical scenario logs the specific failing component's ERROR *before*
   the generic orchestrator abort line, so "first ERROR" lands on the real cause,
   never the wrapper.
2. **Signature** — `sha256(failure_subtype + ":" + failing_service)`.
   `failing_service` comes from the causal line's **logger** (`SptClient` → SPT,
   `JamClient` → JAM, …, falling back to `app_name`). `failure_subtype` is the
   journey's OWN completion outcome, **passed in and never re-derived** — the
   journey mechanism already matched the terminal line, and re-classifying the
   *upstream* causal line would just return `None` and misroute every recognized
   failure to the novel path. `digest is None` ⇔ the novel/embedding path.
3. **INFRA vs order-specific** — decided on the **subtype, not the logger**.
   `AUTH_FAILED` is order-specific (one user's account is disabled) even though its
   causal logger, `JamClient`, is exactly what a naive "client logger ⇒ infra" rule
   would call infrastructure. Only the novel path (no subtype) falls back to message
   *shape*, and a message matching neither shape **defaults to order-specific**:
   worst case a real cross-order outage fragments into per-order incidents (noisy),
   whereas the reverse default would falsely merge unrelated orders and imply a
   shared root cause that doesn't exist.
4. **Match or create.** Order-specific causal lines **never search** — they always
   open their own incident. INFRA searches open incidents: by signature digest when
   there is one, else by **cosine ≥ `INCIDENT_COSINE_THRESHOLD` (0.95)** against the
   primary alert's `embedding`, pre-filtered to the same `failing_service` — then
   **vetoed if the two causal messages disagree on any salient token**, the same
   divergence-guard idea as the semantic cache. Ids are masked before comparing, or
   two identical failures from different orders would read as "diverged" purely
   because their order numbers differ.
5. **Eligibility.** `FAILED` and `TIMED_OUT` journeys, and a `TIMED_OUT` one
   additionally needs a real linked **ERROR** alert (there is no terminal marker to
   anchor a "timed out because X" story on otherwise). Idempotent on `journey_id`:
   a journey whose `incident_id` is set is a no-op.

   **Plus `SUCCESS` journeys that carried a RECOVERED error** (scenario 18) — a
   dependency flapped mid-flow and the order still shipped. These cluster under a
   subtype of their own, **`TRANSIENT_FAILURE`**, and that name is the whole
   mechanism: `sha256("TRANSIENT_FAILURE:SPT")` cannot collide with
   `sha256("ENRICHMENT_FAILED:SPT")`, so a recovered order can **never** land in
   the outage incident and inflate a `journey_count` past the orders that actually
   broke. It is classed INFRA, so several flapping orders merge into ONE incident.
   Without this the journey's alerts kept `incident_id = NULL` forever and no bulk
   action could clear them — `PATCH /incidents/{id}/resolve` cascades on
   `incident_id`, so an alert outside every incident is only ever resolvable one
   click at a time.

   `TRANSIENT_FAILURE` is **never a journey outcome** — it is not written to
   `journeys.outcome` and `status_for` never returns it. The journey stays
   `SUCCESS` because the ORDER succeeded; only the incident records that the
   DEPENDENCY misbehaved. This is the ONE place the incident subtype is not the
   journey's own outcome (`incident_subtype_for`), and the exception is
   deliberate: passing `SUCCESS` through would hash a meaningless `SUCCESS:<svc>`
   and title an incident "SUCCESS — SPT".

   A **clean** success (no ERROR at all) is still ineligible and never clusters —
   gated on the recovered-error flag, not on `SUCCESS`, and answered from the
   journey's in-memory logs so the common healthy path costs no query. The
   `retry_unclustered_completions` sweep now includes `SUCCESS` rows in its
   candidate set for the same reason it includes the others (completion outruns
   alert persistence); on that path the logs are re-read from `journey_events`,
   since the swept completion is rebuilt with an empty log list.
6. **Lifecycle: manual close only.** `PATCH /incidents/{id}/resolve` is the only
   way an incident closes — there is deliberately no automatic mechanism. Resolving
   **cascades to every linked alert** (an incident is a collapsed *view* of those
   alerts; leaving them active would keep them in the live feed and out of History
   forever), reusing the same `is_resolved`/`resolved_at` values the per-alert
   endpoint sets.
7. **The catch-up sweep is not optional.** Clustering runs the instant
   `raw.events` detects a completion — a path that never waits on the LLM — while
   the journey's own alerts only land after `processed.alerts`, which **does** wait
   on the LLM and is bounded by `ALERT_CONCURRENCY`. Under load completion
   routinely outruns alert persistence, so the one shot finds zero causal
   candidates and would skip the journey permanently.
   `retry_unclustered_completions` re-attempts every terminal journey with no
   `incident_id` on the sweep cadence. Found in live testing, not by unit tests
   (which hand-fed already-linked alerts).

Titles are deterministic (`"<subtype> — <service>"`) so they read correctly with
the breaker open; on the novel path the LLM's `suggested_label` is used instead,
**with ids stripped** — an infra incident absorbs many orders over its life, and a
title baked from the first one would go stale and misleading.

Incidents emit `incident.new` / `incident.updated` on the WebSocket (including
from the retry sweep), so an incident's blast radius stays live on screen.
Incidents are **not** sent to Teams.

### Chat feedback (`feedback.py`)
Thumbs up/down on an assistant answer nudges retrieval ranking. Everything here is
pure; the DB access is in `api.py`, the ranking blend in `ai_service/ragindex.py`.
Naive "sort by likes" is worse than no feedback at all, so each bias has a named
countermeasure:

- **Sparse feedback** (most answers are never rated) → a **Bayesian prior**, so an
  unvoted record scores exactly `NEUTRAL` (0.5) — silence is neutral, never negative.
- **Small-sample noise** (1/1 looks better than 8/10) → the same prior pulls small
  samples toward neutral, and `MIN_VOTES` suppresses the signal entirely below a
  handful of votes.
- **Rich-get-richer** (boost → seen more → liked more, freezing early accidents
  into permanent ranking) → a low blend weight (0.15) so relevance still dominates,
  plus `decay_weight` (`HALF_LIFE_DAYS = 30`) so the signal tracks current quality.
- **A vote rates the ANSWER, not any single record** — a downvote may mean the LLM
  worded it badly, not that retrieval was wrong. So votes are stored **per answer**
  (one `chat_feedback` row, upserted on `answer_id` so changing your mind replaces
  rather than stacks) and attributed to records only as a *derivation*
  (`rank_weights` splits the vote by citation rank). Storing per-record would bake
  today's attribution rule into the data permanently.

Votes are the backend's data but ranking happens in the AI service (which must stay
DB-free), so `POST /chat` sends the computed `boosts` **with the request**. If that
query fails, boosts are `{}` and ranking falls back to pure relevance — search must
never break because a vote table is unavailable.

### Pagination (`pagination.py`)
Generic keyset ("seek") pagination for any `ORDER BY <sort> DESC, <id> DESC`
listing — `/alerts`, `/journeys`, `/incidents` all return `Page{items, next_cursor}`.
Keyset over OFFSET because the page after a cursor is one indexed range scan whose
cost doesn't grow with depth, and it's immune to the row-shifting that makes OFFSET
skip or repeat rows when new alerts arrive between requests. `LIMIT limit + 1` is
how a further page is detected without a second COUNT. The cursor is an opaque
url-safe token carrying `(sort_value, id_value)`; datetimes round-trip as ISO-8601.

### PostgreSQL schema (sketch)
```
alerts(alert_id PK, emitted_at, log_id UNIQUE, level, app_name, logger, message,
       event_id, order_id, cart_header_id, account_number,
       explanation, department, severity, source, cached,
       embedding JSONB NULL,                    -- masked-message vector, for clustering
       journey_id FK NULL, incident_id FK NULL,
       is_resolved, resolved_at NULL)
journeys(journey_id PK, status, outcome NULL, first_ts, last_ts,
         event_id, order_id, cart_header_id, summary NULL,
         suggested_failure_label NULL,          -- LLM phrase, novel incident titles
         incident_id FK NULL)
journey_events(id PK, journey_id FK, log_id UNIQUE, ts, raw JSONB)
incidents(incident_id PK, signature NULL, failure_subtype NULL, failing_service NULL,
          error_token NULL,                     -- display only; never in the signature
          title, department NULL, status,        -- status: "open" | "resolved"
          first_ts, last_ts, primary_alert_id FK NULL,
          alert_count, journey_count)
chat_feedback(answer_id PK, created_at, vote SMALLINT, query, record_ids JSONB,
              answer_mode NULL, scoped_kind NULL, scoped_id NULL, username NULL)
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
GET  /alerts                            # 🔒 requires session. Params: since, department,
                                        # source, level, app_name, severity, resolved,
                                        # cached, search, sort, limit, cursor.
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
PATCH /alerts/{alert_id}/resolve        # 🔒 manual triage — is_resolved=True, resolved_at=now()
GET  /journeys?status=&outcome=&search= # 🔒 requires session. status/outcome are
                                        # exact-match FREE STRINGS (not Literals —
                                        # the 10 outcomes are module constants in
                                        # backend/journeys.py and a second list
                                        # would drift), so an unknown value is an
                                        # empty list, not a 422. search is an ILIKE
                                        # substring over event_id/order_id/
                                        # cart_header_id, OR'd as ONE group so it
                                        # ANDs with the other two. Blank = no
                                        # filter. No NULL bucket for outcome —
                                        # status=IN_PROGRESS already selects those.
GET  /journeys/{id}                     # 🔒 journey + its events + summary
GET  /incidents?status=&department=      # 🔒 status is open|resolved; department multi-valued
GET  /incidents/{id}                    # 🔒 incident + its member alerts
PATCH /incidents/{id}/resolve           # 🔒 the ONLY way an incident closes;
                                        # cascades is_resolved to every linked alert
POST /chat                              # 🔒 grounded Q&A over incident history.
                                        # Optional `context` {kind,id} scopes the answer to
                                        # one alert/journey/incident (its text is prepended
                                        # to the query, so retrieval AND the composer are
                                        # both anchored). Mints an `answer_id` for rating.
POST /chat/feedback                     # 🔒 thumbs up/down on one answer_id (upsert)
GET  /stats/insights                    # 🔒 aggregate counters for the insights page
                                        # journeys: total, by_status, by_outcome,
                                        # success_rate (over SUCCESS+FAILED+TIMED_OUT
                                        # only; 0 when none finished), avg_duration_seconds
                                        # alerts: total, open/resolved, by_department,
                                        # by_severity, by_level, by_source
WS   /ws                                # 🔒 alert.new | journey.updated | journey.completed
                                        #    | incident.new | incident.updated
```

`/alerts`, `/journeys` and `/incidents` are **paginated** — they return
`{items, next_cursor}` and take `&limit=&cursor=` (see Pagination above).
`/alerts` also takes `&sort=emitted_at|resolved_at` (the live feed's newest-first
vs History's most-recently-resolved-first). Multi-valued filters are typed as
enums/Literals so FastAPI rejects an out-of-domain value with a 422 per element
before the query runs.

Journey/incident context for `POST /chat` is read **live from Postgres**, not from
the retrieval index: the index has no notion of incidents (an alert is indexed at
persist time, long before clustering runs), and only the DB can give the exact,
current member set instead of a top-k semantic sample. For a journey the selected
log lines (`WARN`/`ERROR` plus first and last) are included, because the summary is
2–4 sentences while the question is usually about the actual sequence.

Alert filter conditions live in ONE place — `alert_filter_conditions()` in
`backend/api.py` returns the WHERE clauses as a list, which `build_alerts_query`
applies wholesale and `/alerts/facets` applies minus one clause per facet. Adding
a filter there reaches both, so the feed and its counts cannot disagree about what
a filter means (a test pins the two endpoints' query params to the same set).
`build_incidents_query` follows the same multi-valued convention: a non-empty list
becomes `IN (...)`, while `None` and `[]` both mean "no filter" — an `IN ()` would
match nothing and empty the page.

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
deploy), `JWT_TTL_SECONDS` (default 8h), `AUTH_COOKIE_SECURE` (true behind TLS),
`AUTH_COOKIE_SAMESITE` (default `lax`).
The dev-run scripts (injector, replay) POST to the collector/RabbitMQ — NOT this
API — so they are unaffected by auth. `passlib` needs `bcrypt<4.1` (pinned).

### Cross-site deployment (e.g. Azure Container Apps)

Locally the dashboard and backend are same-site (`localhost:3000` → `:8000`), so
every default below is the local value and docker-compose needs none of them. In a
deployment where the two get **different hostnames**, three things must change
together — they are one decision, not three:

| Env var | Local default | Cross-site deploy |
|---|---|---|
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | the dashboard's origin (comma-separated for several) |
| `AUTH_COOKIE_SAMESITE` | `lax` | **`none`** |
| `AUTH_COOKIE_SECURE` | `false` | **`true`** (required by `SameSite=none`) |

- **`allow_credentials=True` forbids a `*` origin**, so the dashboard origin must
  be listed explicitly — hence an env var rather than a constant.
- A `lax` cookie is withheld on cross-site fetch **and on the WebSocket upgrade**,
  so login appears to succeed and then every guarded call 401s. `none` fixes that
  but browsers reject `SameSite=None` without `Secure`, so `backend/auth.py`
  validates the pair at import and refuses to start on a bad combination rather
  than shipping a cookie the browser silently drops.
- Set and clear read the same two constants, so a logout can never emit different
  attributes than the login (a mismatch is ignored by the browser and leaves the
  user signed in).
- The Entra `oil_oauth_state` cookie stays `lax` regardless — that hop is a
  top-level redirect, and `strict`/`none` would break or needlessly loosen it.

**TLS to managed backends.** Postgres needs care; Redis and RabbitMQ do not:

- **Postgres** — `DB_SSL=require` turns TLS on without touching the URL. Note
  `?sslmode=require` (the form the Azure portal gives you) is a **trap on
  asyncpg**: SQLAlchemy forwards unknown query params straight to
  `asyncpg.connect()`, which has no `sslmode` parameter (only `ssl`), so it raises
  `TypeError: connect() got an unexpected keyword argument 'sslmode'` on the FIRST
  connection — long after startup looked healthy. `backend/db.py` translates
  `sslmode` → `ssl` for asyncpg and leaves psycopg2 (Alembic's driver, which
  handles `sslmode` natively) alone.
- **Redis** — no change needed: `rediss://` in `REDIS_URL` selects
  `SSLConnection` automatically.
- **RabbitMQ** — no change needed: `amqps://` in `RABBITMQ_URL` enables TLS
  (aiormq branches on the scheme).

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
set) else `general`; `journey.completed` → `general`; `journey.updated` **and
every incident event** → `None` (ignored: per-chunk updates would be spam, and
incidents are a dashboard view, not a notification channel). `backend/main.py`
wires a fan-out `on_event` in its
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
- **Incidents pages** (`/incidents`, `/incidents/[incidentId]`) — the collapsed
  view: one card per incident with its `journey_count`/`alert_count` blast radius,
  live on `incident.new` / `incident.updated`; the detail page lists member alerts
  grouped per order (`OrderGroupRow.tsx`) and offers Resolve
  (`IncidentActionsMenu.tsx`), which cascades to every member alert.
- **Assistant panel** (`components/chat/ChatPanel.tsx` + `lib/chat.tsx`) — one
  drawer, mounted once in `AppShell`, opened from the side-nav, the alert drawer's
  "Ask about this", or a journey view. Opening it from a record passes a **scope**
  so answers are anchored to that alert/journey/incident. Shows each answer's
  `mode` badge (`ai` vs `retrieval-only`, exactly like the alert feed's AI/fallback
  badge), its cited sources with dashboard links, the server-computed coverage
  badge, and thumbs up/down. Deliberately **not persisted** — silently resurrecting
  yesterday's thread about a different order would mislead more than it helps.
  **Documentation sources collapse into ONE `info` badge reading "Official
  documentation"**, never listed individually: a doc citation names a chunk of a
  repository the agent cannot open, so four of them offer nothing to verify and
  push the incident citations — which DO link somewhere — out of sight. They still
  travel in `sources` (no API change; ids stay available for the eval set and the
  network tab), and they are excluded from the `record_ids` sent with a vote, since
  the docs index ranks without feedback and that credit could never be spent.
- **Insights page** (`/insights`) — `GET /stats/insights` rendered as stat tiles
  and breakdown bars (`components/insights/`).
- **Alert detail drawer** (`AlertDetailDrawer.tsx`), **search**
  (`SearchInput.tsx`), and **"load more" pagination** (`lib/usePagination.ts`)
  over the cursor-based endpoints.

---

## Redis keys

| Key | Type | TTL | Purpose |
|---|---|---|---|
| `dedup:{log_id}` | string | 1h | AI-service poller SETNX dedup |
| `ai:last_to` | string | — | poller watermark — `to` of the last fetched window; makes windows contiguous (see [3]). **Load-bearing, not optional.** |
| `ai:breaker:state` | hash | — | circuit breaker state |
| `ai:semcache` | string | — | semantic-cache dump (LRU entries persisted across restarts). Rebuildable — safe to drop. |
| `ai:semcache:hits` / `:misses` | string | — | semantic-cache hit/miss counters (the demo number; `GET /semcache/stats`) |
| `ai:ragindex` | string | — | retrieval-index dump (incident records + vectors). Rebuildable — drop it and run `python -m backend.scripts.backfill_rag`. |
| `ai:llmstats:snapshot` | string | — | LangSmith per-model stats snapshot + its `fetched_at` (all 3 windows x 4 tags), refreshed on a timer and read by `GET /llm-stats`. Rebuildable — safe to drop; it exists so a restart doesn't blank `/ai-performance` until the first cycle lands. |

The **documentation index has no Redis key on purpose** — it is rebuilt from
`ai_service/knowledge/*.md` at every startup in seconds, so a persisted copy could
only drift from the files that are its source of truth.

Journey, incident and feedback state lives in Postgres — the backend owns them.

---

## Running everything

### Native dev (services on the host, infra in Docker)

```bash
docker compose up -d                          # infra only: rabbitmq, redis, postgres
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
python -m pipeline.injector.inject --all      # fire every scenario
python -m backend.scripts.backfill_rag        # optional: index existing history for /chat
pytest                                        # the test suite (pytest.ini: asyncio_mode=auto)
cd dashboard && npm test                      # [6] pure-logic unit tests (node --test)
python -m ai_service.scripts.eval_knowledge   # optional: score docs retrieval (needs the encoder)
```

### Everything in Docker

`docker-compose.yml` publishes infra by default and puts the application services
behind **profiles**, so `up -d` alone stays the native-dev setup:

```bash
docker compose --profile app up -d            # + migrate, collector, mock-services,
                                              #   ai-service, backend, dashboard
docker compose --profile fire up injector     # one-shot: inject --all
```

### Production (Azure VM)

`docker-compose.prod.yml` is **standalone — use it INSTEAD of
`docker-compose.yml`, never merged with it**:

```bash
docker compose -f docker-compose.prod.yml --profile app up -d
```

Differences that matter:

- **One ingress.** Only `caddy` publishes host ports (80/443); everything else is
  reachable solely on the internal Docker network. The dev file publishes
  Postgres/Redis/RabbitMQ/collector/ai-service/backend/dashboard to the host —
  fine on a laptop, not on a box with a public IP. Not publishing 8000 is also
  what makes the backend's `--forwarded-allow-ips=*` safe.
- **One origin.** Caddy serves the dashboard and the backend under a single
  hostname (`Caddyfile`), which makes the session cookie same-site, keeps CORS out
  of the picture, and lets the dashboard bundle build with **relative** URLs
  (`/api`, `/ws`) so no hostname is baked into the image. TLS is automatic
  (Let's Encrypt, needs real DNS for `PUBLIC_HOST` + ports 80/443 reachable); the
  cert lives in the `caddy_data` volume — don't prune it.
- **Survives reboots and long runs.** `restart: unless-stopped`, healthcheck
  gating, and log rotation on every service (Docker's json-file driver is
  unbounded by default and these services log per emitted log line).
- Images are **built in CI and pushed to GHCR** (`.github/workflows/build-images.yml`
  → `oil-app` for all Python services, `oil-dashboard` for Next.js); the VM only
  pulls. Building on the VM would need ~4 vCPUs for torch + the Next bundle and
  would put the private-registry npm token on the box.

> ⚠ **Do not copy a development `.env` onto the VM.** Two services load it
> wholesale via `env_file`, and `env_file` values **win** over the `environment:`
> defaults in the compose file. A leftover `DASHBOARD_URL=http://localhost:3000`
> or `ES_URL=http://localhost:9200` silently replaces the correct value: the stack
> comes up healthy, Teams cards link to localhost, and the poller reads the wrong
> host. Write the VM's `.env` from `.env.example`.

Also required in a deployment: `PASSWORD_LOGIN_ENABLED=false` (the default admin
hash would otherwise be a way around SSO), `AUTH_COOKIE_SECURE=true`, and a real
`JWT_SECRET`.

CI (`.github/workflows/ci.yml`) currently builds the **dashboard only** — the
Python suite is not run there, so run `pytest` locally before pushing.

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
`RAGINDEX_ENABLED=1`, `RAGINDEX_MIN_SCORE=0.30` (recall floor — NOT a precision
gate; see [3]), `RAGINDEX_MAX_ENTRIES=5000`, `RAGINDEX_MODEL=all-MiniLM-L6-v2`
(same value as `SEMCACHE_MODEL` reuses the ONE loaded encoder; a different value
loads a second one), `RAGINDEX_FEEDBACK_WEIGHT=0.15` (0 = feedback off, exact
no-op), `INCIDENT_COSINE_THRESHOLD=0.95` (novel-path incident match),
`DOCSINDEX_ENABLED=1`, `DOCSINDEX_DIR=ai_service/knowledge`,
`DOCSINDEX_MODEL` (defaults to `RAGINDEX_MODEL` — the FOURTH consumer of the one
loaded encoder), `DOCSINDEX_MIN_SCORE=0.20` (lower than the incident floor because
the service+kind narrowing already did the coarse work), `DOCSINDEX_K=4` (its own
budget, so docs and incidents cannot crowd each other out),
`DOCSINDEX_KIND_MODE=soft`,
`AI_SERVICE_URL=http://localhost:8100`, `RAG_INDEX_TIMEOUT=5`,
`RAG_CHAT_TIMEOUT=30`,
`LANGSMITH_API_KEY` + `LANGSMITH_PROJECT` (both required before `/llm-stats`
queries anything; absent = a 200 full of nulls),
`LLM_STATS_REFRESH_INTERVAL_SECONDS=60` (one cycle = 12 requests, so this alone
sets the average rate), `LLM_STATS_STAGGER_SECONDS=1.5` (pause between EVERY
request in a cycle; sized for 12 requests, not 4 — see `settings.py` for the
arithmetic), `LLM_STATS_SNAPSHOT_KEY=ai:llmstats:snapshot`,
`ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` / `ENTRA_CLIENT_SECRET` (all three required
to enable Entra sign-in; absent = disabled, never a crash),
`ENTRA_REDIRECT_URI=http://localhost:8000/auth/entra/callback` (must match the
Azure app registration byte-for-byte), `PASSWORD_LOGIN_ENABLED=true` (set false in
any deployment),
`CORS_ALLOW_ORIGINS=http://localhost:3000` (comma-separated browser origins),
`AUTH_COOKIE_SAMESITE=lax` (`none` for a cross-site deploy — needs
`AUTH_COOKIE_SECURE=true`), `DB_SSL=` (unset = no TLS; `require` for a managed
Postgres) — see "Cross-site deployment" under [5] for how these three go
together — plus Azure AI Foundry vars and the `TEAMS_WEBHOOK_*` webhooks
above.

`LLM_STATS_CACHE_TTL_SECONDS` was **removed** with the TTL cache it belonged to —
reads no longer fetch, so there is nothing to expire.

---

## Testing

- **Correlation invariants**: phase-1 logs (through `create`, incl. Inbound's
  whole Settings/JAM/SOLR leg and the `order_data_ready` ack) never contain
  order-id *fields*; phase-2 logs never contain `eventId`; **no single line
  links both id families as fields**; the eventId→order-id join is mined from
  the order-engine creation logs' text; pre-creation failures (4, 5, 9,
  15/16/17) produce eventId-only journeys; a stray 19-digit number in prose
  never merges journeys.
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
- **Journey rules**: each canonical scenario (1–10) ends with its expected
  outcome; a journey whose LAST line is Track & Trace's `"Registered order ...
  for tracking"` is still IN PROGRESS (never SUCCESS); killing the chain mid-flow
  (drop the baton) produces `TIMED_OUT` after 90s; a stalled journey that DID log
  an unrecognized ERROR is `FAILED`/`UNRECOGNIZED_FAILURE`, not `TIMED_OUT`.
- **Incident clustering**: one order's many alerts collapse to ONE incident;
  scenarios 8+11+12 merge into one INFRA incident (`journey_count=3`) while 6+13
  stay separate (order-specific never merges); `AUTH_FAILED` classifies
  order-specific despite its `JamClient` logger; the novel path merges 15+16 by
  cosine but the divergence guard vetoes 17; a journey clustered twice is a no-op
  (idempotent on `journey_id`); resolving an incident cascades `is_resolved` to its
  alerts; `retry_unclustered_completions` picks up a journey whose completion
  outran its alerts.
- **Retrieval + chat**: retrieval returns `[]` (never raises) with no encoder;
  upsert-by-id means a re-index replaces rather than duplicates; the `min_score`
  floor is applied to raw cosine so feedback can't lift a sub-floor record;
  `feedback_weight=0` is a byte-identical no-op; an unvoted record scores
  `NEUTRAL`; `/chat` degrades to `mode="retrieval-only"` with the breaker open and
  still returns the same sources; `coverage.truncated` is true exactly when
  `shown == limit`; an index push failure never fails alert persistence or journey
  completion.
- **Documentation RAG** (`tests/test_knowledge_loader.py`, `test_knowledge_routing.py`,
  `test_docsindex.py`, `test_chat_docs.py`, `test_chat_grounding.py`): every chunk
  fits the encoder budget (over-budget text is truncated with NO error, so this is
  the guard against silent loss) and all 17 of §11's traps survive the split;
  excluded sections (§7 and its children, §13) produce no chunks; `answering-policy.md`
  is skipped for the STRUCTURAL reason (no frontmatter), not by filename; ids are
  content-derived and unique; a table row keeps its header; `kind` is right for both
  wordings of §1/§3/§4; a nested section inherits its parent's exclusion; an
  unrecognised heading is indexed unlabelled, not dropped; chunking is deterministic.
  Routing: a short alias (`oe`) never matches inside a word; an undocumented service
  routes to its `see_also` doc, or returns `[]` when it has none; a documented
  service beats an undocumented one; soft mode keeps non-matching kinds reachable
  while hard mode drops them. Index: every failure mode (no encoder, missing folder,
  empty corpus, unconfigured) disables it rather than raising, and a FAILED reload
  keeps the previous index. Chat: docs alone suffice to compose; `self_grounded`
  composes with zero sources while a bare question still refuses and never calls the
  model; `coverage` still counts incidents only; incident filters never reach the
  docs channel; no record id survives into the prose while `ORD-…` always does.
  `tests/data/knowledge_eval.yaml` is pinned in CI (every expected chunk id must
  exist, every kind must have a question) — the scored run needs the encoder and is
  manual.
- **Feedback**: silence scores neutral, not negative; 1/1 ranks below 8/10;
  `MIN_VOTES` suppresses a single stray click; a re-vote replaces rather than
  stacks; decay ages votes out.
- **Pagination**: a cursor round-trips (including datetimes); a page of exactly
  `limit` rows still reports `next_cursor`; the last page reports `None`; rows
  inserted between requests neither skip nor repeat.
- **End-to-end**: `injector --all` → one journey per scenario with the exact
  outcomes above (9 and 15/16/17 as eventId-only journeys), alerts visible on WS,
  journey completions with summaries, incidents formed with the merge/separate
  pattern in the scenario table.

## Gotchas / rules for future changes

- **Never** correlate by `accountNumber`.
- **Never** assume `orderId` exists at the start of a journey — pre-creation
  failures live and die with only `eventId`. Under the five-hop flow that
  includes the Settings (15/16/17) and JAM (9) failures, not just transform
  and creation.
- There is **no creation-response bridge** — do NOT reintroduce one, and do
  not add order ids (fields or text) to the `order_data_ready` ack. The
  eventId→order-id join comes ONLY from mining the order-engine creation
  logs' message text (`backend/stitching.py`).
- Message texts are load-bearing in **two** ways: journey terminal detection
  matches on them (`backend/journeys.py`) AND id mining extracts eventId/orderId/
  cartHeaderId from them (`backend/stitching.py`). Changing a service block's
  message — especially the order-engine `create` logs, Inbound's `close`
  terminal, or any fatal-abort line — requires updating the detection rules,
  the mining patterns, and their tests together. Track & Trace's
  `"Registered order ... for tracking"` must never become a terminal again —
  it fires before the checks have run.
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
  embedding model (sentence-transformers) is local, not a provider — it lives in
  `ai_service/semcache.py`, not `llm.py`, and `ragindex.py` **imports** it rather
  than loading a second copy.
- **Incident clustering classifies on the failure's own SUBTYPE, never the causal
  logger.** `AUTH_FAILED`'s causal logger is `JamClient`, which a "client logger ⇒
  infra" rule would wrongly call infrastructure and merge across orders. And never
  re-run `classify_failure` inside `incidents.py` — the subtype is passed in from
  the journey's completion; re-classifying the upstream causal line returns `None`
  and dumps every recognized failure onto the novel path.
- **Unknown clustering shapes default to order-specific, never INFRA.** A wrong
  fragment is noisy; a wrong merge asserts a shared root cause that doesn't exist.
- **A recovered journey clusters as `TRANSIENT_FAILURE`, never as the failure
  subtype for the same service.** The distinct subtype is what keeps the digests
  apart; reusing `ENRICHMENT_FAILED` for a blip would merge a shipped order into
  the outage incident and make every `journey_count` on the dashboard mean
  "orders that touched a broken service" instead of "orders that broke". And
  `TRANSIENT_FAILURE` must stay OUT of `journeys.outcome` — the order succeeded;
  only the incident says a dependency flapped.
- **Clean successes must never cluster.** `SUCCESS` eligibility is gated on the
  journey having an ERROR that RECOVERED, not on `SUCCESS` itself — otherwise
  every healthy order would attempt to cluster on every run and find no causal
  line. Keep that check answerable from the in-memory logs on the live path.
- **`UNRECOGNIZED_FAILURE` ≠ `TIMED_OUT`.** The first means a real ERROR nothing
  matched; the second means silence. Clustering treats them differently
  (`TIMED_OUT` needs a linked ERROR to be eligible at all), and collapsing the two
  would break the novel/embedding path.
- **Don't remove `retry_unclustered_completions`.** Completion detection
  (`raw.events`, no LLM) routinely outruns alert persistence
  (`processed.alerts`, LLM-bound), so the one-shot clustering pass legitimately
  finds nothing and the sweep is the only thing that catches those journeys.
- Incidents close **only** through `PATCH /incidents/{id}/resolve` — there is
  deliberately no automatic close. Resolving must keep cascading to member alerts,
  or they never leave the live feed.
- The retrieval index and the semantic cache share an encoder but **not a
  threshold** (0.30 recall floor vs 0.95 near-identical). Do not unify them: the
  cache must fail toward a miss, retrieval wants recall. The docs index is the
  FOURTH consumer of that one encoder and has its own floor (0.20) and its own `k`.
- **Never merge the docs index back into `ragindex`.** Its eviction is oldest-first,
  and docs — loaded once at startup — are permanently the oldest entries, so they
  would be evicted within days with no error at all. Separate budgets are also the
  only thing that guarantees an answer gets both documentation and incidents.
- **Never swap the encoder to "fix" chunk truncation.** A bigger context window
  leaves the real problem — one chunk is one vector, so a section holding 17
  unrelated warnings means nothing in particular — and it invalidates every vector
  already persisted in `alerts.embedding` while de-tuning `SEMCACHE_THRESHOLD`,
  `INCIDENT_COSINE_THRESHOLD` and `RAGINDEX_MIN_SCORE`, all chosen for THIS model.
- **Doc chunk ids are content-derived, never positional**, and section `kind` comes
  from heading TEXT, never the section number. Both exist because the template will
  be renumbered, and an ordinal id silently re-points citations already shown to a
  user and invalidates the evaluation set.
- **The corpus must stay in git and inside `ai_service/`.** Not tracked ⇒ not in the
  image ⇒ an empty index in production, silently. `build-images.yml` filters on
  `ai_service/**` so a docs-only edit rebuilds the image, and `.dockerignore`
  excludes only `docs/` and `ways-of-working/` — moving the corpus into a name
  inside that block stops it reaching the build context.
- **Zero retrieved sources must not mean "refuse"** — see `self_grounded` in [3].
  The guard against composing with nothing to ground in stays, but "grounded" is not
  a synonym for "retrieval returned rows" now that a caller supplies its own context.
- **Record ids never appear in answer prose.** The prompt forbids them and
  `clean_answer_citations()` enforces it deterministically. Business identifiers
  (`ORD-…`, `evt-…`, cart header ids) are explicitly NOT record ids — stripping
  those would gut the answer.
- **Feedback is a nudge, never a gate.** Keep the relevance floor on raw cosine,
  keep unvoted records at `NEUTRAL`, and keep the blend weight low — every one of
  those is a countermeasure to a specific documented bias (see [5] `feedback.py`).
  Search must still work when the vote table doesn't (`boosts = {}`).
- Indexing is **fire-and-forget**: it sits on the alert-persist and
  journey-completion paths, so every failure is logged and swallowed. A stale index
  is fixed by re-running the backfill, never by failing a write.
- The backend must stay **DB-owner** and the AI service **encoder-owner**: ranking
  boosts travel with the `/chat` request rather than the AI service reading
  Postgres, and `/index` metadata stays free-form so a new filter key needs no
  AI-service change.
- Auth methods are added, never rewired: a new one mints the same JWT via
  `issue_token` and sets the same cookie via `set_auth_cookie`. Never make a
  route, the WS handshake, or the dashboard aware of *how* someone signed in.
  The Entra `state`/`nonce` cookie must stay `SameSite=lax`, and MSAL calls must
  stay off the event loop (`asyncio.to_thread`).
- All datetimes are UTC and timezone aware (timestamptz in Postgres,
  `datetime.now(timezone.utc)` in Python — never `utcnow()`, never naive
  datetimes). The 90s stalled-journey arithmetic depends on this.
- Deployment config is **two standalone compose files**, not a base + override:
  never `-f docker-compose.yml -f docker-compose.prod.yml`. And `env_file` beats
  `environment:`, which is why a laptop `.env` on the VM breaks it silently.