# Order Intelligence Platform

## What this is

Internal e-commerce platform where **sales employees** order hardware (laptops, GPUs, multiple brands).
When an order fails somewhere in the pipeline, a WARN/ERROR log lands in Elasticsearch.
**Today**: IT support reads it and manually forwards it to the right team (devops / networking / DBA).
**This project**: the AI Service (LangGraph) explains the issue in plain English and routes it
directly to the right engineering team — WebSocket dashboard + Slack — with no manual triage.

8-day POC, 3 developers, monorepo. Detailed day-by-day plan: `docs/plan.md`.

## Architecture

```
Sales UI → Order Engine → RabbitMQ [orders.*] → Validator / RSM-SPT / Avalara
                                                    └→ WARN/ERROR logs → Elasticsearch

AI Service ← polls ES, sliding window [time()-25s, time()-5s] ← Elasticsearch
AI Service → RabbitMQ [issues.raw] (TTL, DLQ)      # buffer: fast polling / slow LLM
AI Service ← consumes issues.raw one at a time:
    Redis dedup HIT  → ACK and drop (already processed)
    Redis dedup MISS → LangGraph (ExplainerNode → RouterNode) → RabbitMQ [issues.processed]
Core Backend ← consumes issues.processed
Core Backend → order_issues (PostgreSQL) + WebSocket (team dashboards) + Slack webhook
Core Backend → ES via HTTP, on demand only, for GET /journeys/{trace_id}
```

- **One RabbitMQ broker, distinct queues.** `orders.*` (order flow between microservices), `issues.raw` (AI ingestion buffer, has TTL + DLQ), `issues.processed` (AI results for Core Backend). The two RabbitMQ boxes in the diagram are the same broker.
- **AI Service owns issue ingestion end-to-end**: polls ES, self-publishes raw logs to `issues.raw`, consumes them back one at a time. This buffer decouples fast polling from slow LLM calls, and unacked messages survive an AI Service crash.
- **Duplicates are expected** — sliding windows overlap and the same error can appear in consecutive polls. Redis dedup key: hash of normalized log + `trace_id`. On hit: ACK, drop, no LLM call. Redis also caches LLM explanations for similar (not identical) errors.
- LLM calls go through a circuit breaker; when open, publish a fallback explanation routed to IT_SUPPORT.
- Every RabbitMQ message and every log line carries `trace_id` — the join key for journey assembly.
- No Elasticsearch access yet: `ai-service/app/polling/` uses an abstract interface with a mock reading `infra/fixtures/es_events.json`. Real ES REST implementation plugs in without touching other code. Core Backend's on-demand journey lookup uses the same pattern.

## User roles

| Role | Can do |
|------|--------|
| `SALES` | Create orders, view own order status |
| `IT_SUPPORT` | View ALL issues, override AI routing, see audit trail |
| `ENGINEER` (team: `devops` \| `networking` \| `database_admin`) | View issues routed to their team, ack/resolve |

Auth: JWT with `role` + `team` claims. Simple email+password for POC. Enforce role checks in FastAPI dependencies (`app/api/deps.py`), never only in frontend.

## Tech stack

- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async + asyncpg, aio-pika, Alembic
- LangGraph (AI Service only) — Claude via Azure AI Foundry
- Next.js 14 App Router, TypeScript, Tailwind
- PostgreSQL 16, Redis 7, RabbitMQ 3 — all Docker, no cloud DB
- pytest + pytest-asyncio, ruff (lint + format), mypy

## Repository layout

```
order-intelligence/
├── CLAUDE.md
├── docker-compose.yml
├── docker-compose.dev.yml
├── .env.example
├── Makefile
├── README.md
├── .gitignore
│
├── .claude/
│   ├── settings.json
│   ├── commands/
│   │   ├── review.md
│   │   ├── test.md
│   │   └── new-service.md
│   └── skills/                        # installed via npx skills add
│
├── docs/
│   ├── plan.md
│   ├── message-contracts.md
│   └── adr/
│       └── 001-polling-over-streaming.md
│
├── shared/
│   ├── __init__.py
│   ├── base_worker.py                 # abstract RabbitMQ consumer — all microservices extend this
│   ├── publisher.py                   # standalone RabbitMQ publisher
│   ├── logging_config.py              # structured JSON logging with trace_id
│   ├── sanitize.py                    # strip secrets/PII before LLM prompts
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── order_event.py             # RabbitMQ message envelope (trace_id, timestamp, severity)
│   │   ├── order_issue.py             # what gets written to order_issues DB
│   │   ├── ai_explanation.py          # ExplainerNode output
│   │   ├── ai_routing.py             # RouterNode output (Team enum + confidence)
│   │   ├── notification.py            # Slack/Teams webhook payload
│   │   └── validation.py             # Validator Service result
│   └── tests/
│       ├── __init__.py
│       ├── test_schemas.py
│       └── test_base_worker.py
│
├── order-engine/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                    # FastAPI app + lifespan
│   │   ├── config.py                  # pydantic-settings
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   └── order.py               # order domain model
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   └── order_service.py       # create order, generate trace_id
│   │   └── api/
│   │       ├── __init__.py
│   │       ├── deps.py                # FastAPI dependencies (auth, db)
│   │       └── routes/
│   │           ├── __init__.py
│   │           ├── orders.py          # POST /orders, GET /orders/{id}
│   │           └── health.py
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       └── test_order_service.py
│
├── validator-service/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── consumer.py                # extends BaseWorker
│   │   ├── publisher.py
│   │   ├── rules/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py              # rule engine
│   │   │   └── validators.py          # business rules (stock, budget, etc.)
│   │   └── logging/
│   │       ├── __init__.py
│   │       └── es_logger.py           # log WARN/ERROR to Elasticsearch
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       └── test_validators.py
│
├── rsm-spt/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── consumer.py                # extends BaseWorker
│   │   ├── publisher.py
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── pricing.py
│   │   │   └── stock.py
│   │   └── logging/
│   │       ├── __init__.py
│   │       └── es_logger.py
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       └── test_pricing.py
│
├── avalara/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── consumer.py                # extends BaseWorker
│   │   ├── publisher.py
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   └── tax_calculator.py
│   │   └── logging/
│   │       ├── __init__.py
│   │       └── es_logger.py
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       └── test_tax_calculator.py
│
├── ai-service/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── graph.py                   # LangGraph StateGraph definition + compile
│   │   ├── state.py                   # IssueState Pydantic BaseModel
│   │   ├── polling/
│   │   │   ├── __init__.py
│   │   │   ├── base.py               # abstract poller interface (swap mock ↔ real ES)
│   │   │   ├── elasticsearch.py      # real ES REST poller (when access arrives)
│   │   │   ├── mock_poller.py        # reads infra/fixtures/es_events.json
│   │   │   └── scheduler.py          # sliding window loop [time()-25s, time()-5s]
│   │   ├── nodes/
│   │   │   ├── __init__.py
│   │   │   ├── explainer.py           # ExplainerNode — LLM → natural language explanation
│   │   │   └── router_node.py         # RouterNode — LLM → Team enum + confidence
│   │   ├── prompts/
│   │   │   ├── __init__.py
│   │   │   ├── explainer.py           # system prompt for ExplainerNode
│   │   │   └── router.py             # system prompt for RouterNode
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   ├── client.py             # Azure AI Foundry / Claude client
│   │   │   └── circuit_breaker.py    # pybreaker wrapper
│   │   ├── messaging/
│   │   │   ├── __init__.py
│   │   │   ├── consumer.py           # consumes issues.raw (self-published by poller)
│   │   │   └── publisher.py          # publishes to issues.raw (poller) + issues.processed (results)
│   │   └── cache/
│   │       ├── __init__.py
│   │       ├── dedup.py              # processed-issue dedup: hash(normalized log + trace_id)
│   │       └── redis.py              # Redis client + LLM response cache for similar errors
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       ├── fakes.py                   # mock LLM client — never call real LLM in tests
│       ├── test_polling/
│       │   └── test_scheduler.py
│       ├── test_dedup.py
│       ├── test_explainer.py
│       ├── test_router_node.py
│       ├── test_circuit_breaker.py
│       └── test_graph.py
│
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── migrations/
│   │   ├── env.py
│   │   └── versions/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                    # FastAPI app + lifespan (startup/shutdown)
│   │   ├── config.py
│   │   ├── messaging/
│   │   │   ├── __init__.py
│   │   │   ├── consumer.py           # consumes issues.processed (AI Service results)
│   │   │   └── dlq_handler.py        # DLQ monitoring + reprocessing (all queues)
│   │   ├── journeys/
│   │   │   ├── __init__.py
│   │   │   ├── es_client.py          # on-demand ES HTTP lookup by trace_id (mockable)
│   │   │   ├── assembler.py          # collect events by trace_id → journey timeline
│   │   │   └── models.py             # internal journey domain models
│   │   ├── notifications/
│   │   │   ├── __init__.py
│   │   │   ├── slack.py              # HTTP POST → Slack webhook
│   │   │   ├── teams.py             # HTTP POST → MS Teams webhook
│   │   │   └── router.py            # decide where to notify based on team/severity
│   │   ├── websocket/
│   │   │   ├── __init__.py
│   │   │   ├── manager.py           # WebSocket connection manager
│   │   │   └── events.py            # push real-time updates to connected clients
│   │   ├── db/
│   │   │   ├── __init__.py
│   │   │   ├── session.py           # async SQLAlchemy session factory
│   │   │   ├── models.py            # ORM: users, orders, order_issues
│   │   │   └── repository.py        # CRUD for order_issues (single write point)
│   │   └── api/
│   │       ├── __init__.py
│   │       ├── deps.py              # FastAPI deps: get_db, get_current_user, require_role
│   │       └── routes/
│   │           ├── __init__.py
│   │           ├── issues.py        # GET /issues, GET /issues/{id}, PATCH /issues/{id}/resolve
│   │           ├── journeys.py      # GET /journeys/{trace_id}
│   │           ├── auth.py          # POST /auth/login, POST /auth/register
│   │           └── health.py
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       ├── test_journeys/
│       │   ├── test_assembler.py
│       │   └── test_es_client.py
│       ├── test_messaging/
│       │   ├── test_consumer.py
│       │   └── test_dlq_handler.py
│       └── test_notifications/
│           └── test_slack.py
│
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── next.config.js
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx              # login / landing
│   │   │   ├── dashboard/
│   │   │   │   └── page.tsx          # real-time issues feed (role-filtered)
│   │   │   ├── issues/
│   │   │   │   ├── page.tsx          # issues list (historical, filterable)
│   │   │   │   └── [id]/
│   │   │   │       └── page.tsx      # issue detail: journey timeline + AI explanation
│   │   │   ├── orders/
│   │   │   │   ├── page.tsx          # sales: order list
│   │   │   │   └── new/
│   │   │   │       └── page.tsx      # sales: create new order
│   │   │   └── journeys/
│   │   │       └── [traceId]/
│   │   │           └── page.tsx      # full order journey explorer
│   │   ├── components/
│   │   │   ├── ui/                   # generic: buttons, cards, modals, badges
│   │   │   ├── journey-timeline.tsx
│   │   │   ├── issue-card.tsx
│   │   │   ├── live-feed.tsx         # WebSocket real-time feed
│   │   │   ├── status-badge.tsx
│   │   │   ├── order-form.tsx
│   │   │   └── nav-bar.tsx           # role-aware navigation
│   │   ├── hooks/
│   │   │   ├── use-websocket.ts
│   │   │   ├── use-issues.ts
│   │   │   └── use-auth.ts
│   │   ├── lib/
│   │   │   ├── api.ts               # HTTP client (axios/fetch) for backend REST
│   │   │   ├── ws.ts                # WebSocket client with reconnection
│   │   │   └── auth.ts             # JWT storage, refresh, role helpers
│   │   ├── types/
│   │   │   └── index.ts             # TypeScript types mirroring shared/schemas
│   │   └── context/
│   │       └── auth-context.tsx     # React context for current user + role
│   └── tests/
│
└── infra/
    ├── rabbitmq/
    │   └── definitions.json          # exchanges, queues, bindings, DLQ, TTL config
    ├── postgres/
    │   ├── init.sql                  # schema bootstrap (runs on first docker compose up)
    │   └── demo-seed.sql            # realistic demo data
    ├── redis/
    │   └── redis.conf
    ├── fixtures/
    │   └── es_events.json           # mock Elasticsearch WARN/ERROR events for dev
    └── nginx/
        └── nginx.conf               # reverse proxy (optional, for demo)
```

## Message contract (never bypass)

All inter-service messages are `shared.schemas.OrderEvent`:
`trace_id: str` (UUID, created once by Order Engine, immutable), `timestamp: datetime`,
`source_service: str`, `event_type: str` (dot notation: `order.created`, `validation.failed`),
`severity: "INFO"|"WARN"|"ERROR"`, `payload: dict`.
AI results are `shared.schemas.AIExplanation` and `shared.schemas.AIRouting`
(`assigned_team` is a `Team` enum — never a free string).
Change a schema = update `docs/message-contracts.md` + tests in the same PR.

## AI Service rules (ingestion + LangGraph)

Ingestion pipeline (order matters):
1. `polling/scheduler.py` polls ES every ~20s, window `[time()-25s, time()-5s]`, filter WARN/ERROR only.
2. Every found log → publish raw to `issues.raw` (with TTL + DLQ). No processing at poll time.
3. `messaging/consumer.py` consumes `issues.raw` one at a time (prefetch_count=1).
4. Dedup check first (`cache/dedup.py`): key = hash(normalized log + trace_id). HIT → **ACK immediately, no LLM call**. MISS → run the graph, mark key in Redis (with TTL), then ACK.
5. Graph result → publish to `issues.processed`. ACK the raw message only after successful publish.

LangGraph rules:
- Graph state is a **Pydantic BaseModel** (`IssueState`), not TypedDict — we want validation between nodes.
- `ExplainerNode`: system prompt in `ai-service/app/prompts/explainer.py`. Input: raw log + trace context. Output: 2-4 sentence explanation a non-expert understands.
- `RouterNode`: uses `llm.with_structured_output(AIRouting)` — output is always a valid `Team` enum + confidence + reasoning. Low confidence (<0.6) routes to IT_SUPPORT for manual triage.
- Nodes return partial state updates; never mutate state in place.
- Every LLM call: through `app/llm/client.py` (circuit breaker wrapped). Never call the SDK directly from a node.
- Prompts never contain: credentials, connection strings, customer PII, full DB dumps. Pass sanitized log excerpts only (`shared/sanitize.py` strips secrets by regex before any prompt).

## Database essentials

Tables: `users` (id, email, hash, role, team), `orders` (id, trace_id, sales_user_id, items JSONB, status),
`order_issues` (id, trace_id, source_service, severity, ai_explanation, assigned_team, routing_confidence,
status: open/acked/resolved, raw_event JSONB, created_at).
Migrations: Alembic only — never hand-edit a deployed schema. `alembic upgrade head` runs on backend startup.
Demo reset: `make demo-reset` = `docker compose down -v && up && seed from infra/postgres/demo-seed.sql`.

## Commands

```
make up               # docker compose up all services
make down             # stop everything
make demo-reset       # wipe volumes, recreate schema, load demo seed data
make test s=backend   # pytest for one service
make test-all         # pytest for every service (CI runs this)
make lint             # ruff check + format --check + mypy, all services
make logs s=ai-service
make send-order       # POST a sample order to Order Engine (smoke test)
make break-order      # POST an order crafted to fail validation (demo the AI flow)
```

## Code conventions

- Async everywhere: async SQLAlchemy, aio-pika, httpx. A sync call in a request path is a review blocker.
- All cross-service data uses `shared/schemas` models. Inline dicts crossing a boundary = review blocker.
- Type hints on every function. `Any` requires a `# why:` comment.
- Business logic lives in `services/` or `journeys/`, not in route handlers or consumers.
- Config via pydantic-settings from env vars only. No hardcoded URLs, ports, keys.
- Every log line: structured (JSON), includes `trace_id` when one exists.
- Frontend: server components by default; client components only for WebSocket/live UI. Types in `frontend/src/types` mirror `shared/schemas` — update together.

## Testing requirements (from Ways of Working — gate for every merge)

- New logic ships with tests in the same PR. Failure paths are mandatory for: circuit breaker open, RabbitMQ down, malformed OrderEvent, LLM timeout, low routing confidence.
- LLM is always mocked in tests (`ai-service/tests/fakes.py`). No live LLM calls in CI.
- Run the affected service's tests + `make lint` before every commit.

## Git workflow (from Ways of Working)

- Feature branches → PR → 1 human review minimum → merge. Never push to main.
- Tag PRs `ai-first` when Claude materially wrote the code. AI is never a commit co-author.
- The PR author must be able to explain every changed line. "It works" is not an explanation.
- One thin slice per PR (one consumer, one endpoint, one component) — not a whole service.

## Never do

- Never commit secrets, `.env`, or real API keys (use `.env.example` as template).
- Never put PII, credentials, or raw DB dumps in an LLM prompt — sanitize first.
- Never bypass RabbitMQ with direct HTTP calls between microservices.
- Never write to `order_issues` from anywhere except Core Backend's repository layer.
- Never let frontend trust its own role checks — the API enforces authz.
- Never merge with failing tests, failing lint, or an unexplained diff.

## Deeper docs (read when relevant)

- `docs/plan.md` — 8-day plan, who owns what, critical path
- `docs/message-contracts.md` — full event catalog with examples
- `docs/adr/` — architecture decision records (why polling not streaming, why circuit breaker, etc.)
- `infra/fixtures/es_events.json` — realistic WARN/ERROR events for dev without ES access
