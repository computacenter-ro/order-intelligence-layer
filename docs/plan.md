# Order Intelligence Platform — 8-Day Build Plan

## Team Roles

| Person | Focus | Owns |
|--------|-------|------|
| **A — Backend & Infra** | Core Backend, Docker, RabbitMQ topology, PostgreSQL | Core Backend, infra/, docker-compose |
| **B — AI & Microservices** | AI Service (LangGraph), Validator, RSM/SPT, Avalara | ai-service/, validator/, rsm-spt/, avalara/ |
| **C — Frontend & Integration** | Next.js dashboard, WebSocket, REST client, shared schemas | frontend/, shared/schemas/, integration tests |

Everyone touches `shared/` — that's the contract layer. No one owns it alone.

---

## Days 1–2: Build the Common Foundation Together

The mentor is right — if you separate too early, you'll spend Day 5 debugging incompatible message formats. Days 1–2 are **all three people, same room, same codebase.**

### Day 1 — Scaffold + Shared Contracts

**All three together.**

Morning — project skeleton:
- Initialize monorepo, git, `.claude/`, `CLAUDE.md`
- `docker-compose.dev.yml` with: RabbitMQ (management UI on :15672), Redis, PostgreSQL
- Run `docker compose up` — confirm all three are reachable
- Agree on `.env.example` with all connection strings

Afternoon — shared schemas (the most important work of Day 1):
- `shared/schemas/order_event.py` — the RabbitMQ message envelope:
  ```python
  class OrderEvent(BaseModel):
      trace_id: str           # UUID, created by Order Engine, never changes
      timestamp: datetime
      source_service: str     # "order-engine", "validator", "rsm-spt", "avalara"
      event_type: str         # "order.created", "validation.passed", "validation.failed", ...
      payload: dict           # service-specific data
      ttl: int | None = None
  ```
- `shared/schemas/order_issue.py` — what gets written to `order_issues` DB
- `shared/schemas/ai_explanation.py` — ExplainerNode output
- `shared/schemas/ai_routing.py` — RouterNode output (team assignment)
- `shared/schemas/notification.py` — Slack/Teams payload
- Write tests for every schema — `pytest shared/tests/`

End of Day 1 deliverable:
- [ ] `docker compose up` works — RabbitMQ, Redis, PostgreSQL running
- [ ] All shared Pydantic schemas defined, validated, tested
- [ ] Monorepo structure matches the agreed architecture
- [ ] Everyone can run `pytest shared/tests/` and it passes

### Day 2 — Base Worker + First Message Flow

**All three together — this is the day that defines the communication pattern for every microservice.**

Morning — base worker:
- `shared/base_worker.py` — abstract RabbitMQ consumer using `aio-pika`:
  ```python
  class BaseWorker(ABC):
      async def start(self, queue_name: str, exchange_name: str)
      async def stop(self)
      @abstractmethod
      async def process_message(self, event: OrderEvent) -> OrderEvent | None
      async def publish(self, event: OrderEvent, routing_key: str)
  ```
- This handles: connection management, deserialization into `OrderEvent`, `trace_id` logging, error handling, DLQ republishing on failure, graceful shutdown
- `shared/publisher.py` — standalone publisher for services that only send
- `shared/logging_config.py` — structured logging with `trace_id` in every line

Afternoon — prove the pattern works end-to-end:
- Scaffold `order-engine/` with a FastAPI endpoint that publishes an `OrderEvent` to RabbitMQ
- Scaffold a minimal `validator-service/` that extends `BaseWorker`, consumes the event, logs it, publishes a result
- Run both with Docker — send a POST to Order Engine, see Validator pick it up
- **This is the "hello world" of your architecture.** If this works, every other service is the same pattern.

End of Day 2 deliverable:
- [ ] `BaseWorker` implemented and tested
- [ ] Order Engine publishes to RabbitMQ
- [ ] Validator Service consumes, processes, republishes
- [ ] Full message flow visible in RabbitMQ Management UI
- [ ] Structured logging with `trace_id` works
- [ ] Everyone understands the pattern — anyone can now build a microservice

---

## Days 3–5: Separate and Build in Parallel

Now you split. Each person owns their services but checks in at dailies.

### Day 3 — Core Services

**Person A — Core Backend foundation:**
- `backend/app/main.py` — FastAPI with lifespan
- `backend/app/db/` — SQLAlchemy async setup, `order_issues` table, repository
- `backend/app/messaging/consumer.py` — extends `BaseWorker`, consumes `issues.processed` (AI Service results), writes each to `order_issues`
- `backend/app/journeys/es_client.py` — on-demand ES HTTP lookup by `trace_id` (abstract interface + mock reading fixtures, real REST impl when access arrives)
- `backend/app/journeys/assembler.py` — events from es_client → journey timeline
- REST endpoint: `GET /api/issues` — returns issues from DB
- REST endpoint: `GET /api/journeys/{trace_id}` — returns assembled journey

**Person B — AI Service ingestion pipeline + LangGraph skeleton:**

The AI Service is the front door for issues, so the ingestion pipeline comes first:
- `ai-service/app/polling/scheduler.py` — sliding window loop `[time()-25s, time()-5s]`, every ~20s
- `ai-service/app/polling/mock_poller.py` — reads `infra/fixtures/es_events.json` (abstract interface in `base.py`, real ES REST impl plugs in later)
- Poller publishes every WARN/ERROR log raw to `issues.raw` (TTL + DLQ configured)
- `ai-service/app/messaging/consumer.py` — consumes `issues.raw` one at a time (prefetch_count=1)
- `ai-service/app/cache/dedup.py` — Redis dedup: key = hash(normalized log + trace_id). HIT → ACK and drop. MISS → process, mark key, ACK
- LangGraph skeleton with **fake LLM**: `graph.py` + `state.py` (IssueState Pydantic model) + `nodes/explainer.py` + `nodes/router_node.py` returning canned responses
- End of day: mock poller → issues.raw → dedup → fake graph → `issues.processed`, full loop working without a real LLM
- Tests: scheduler window math, dedup hit/miss, graph with fakes

**Person C — Frontend scaffold + first pages:**
- `npx create-next-app frontend` with TypeScript, Tailwind
- `frontend/src/types/index.ts` — TypeScript types mirroring `shared/schemas/`
- `frontend/src/lib/api.ts` — HTTP client for backend REST
- `frontend/src/app/page.tsx` — dashboard layout (empty state, loading states)
- `frontend/src/app/issues/page.tsx` — issues list, fetching from `GET /api/issues`
- `frontend/src/components/issue-card.tsx`
- `frontend/src/components/status-badge.tsx`
- Use mock data until backend REST is ready — define the mocks from `shared/schemas/`

### Day 4 — Features

**Person A — Real-time + notifications:**
- `backend/app/websocket/manager.py` — WebSocket connection manager
- `backend/app/websocket/events.py` — push new issues to connected clients in real-time
- `backend/app/notifications/slack.py` — HTTP POST to Slack webhook
- `backend/app/notifications/teams.py` — HTTP POST to Teams webhook
- `backend/app/notifications/router.py` — decide where to notify based on severity/team
- `backend/app/journeys/detector.py` — detect anomalies: timeout between steps, missing events, DLQ entries
- Tests for journey detection logic

**Person B — Real LLM + remaining microservices:**
- `ai-service/app/llm/client.py` — Azure AI Foundry / Claude client, replaces fake LLM in the graph
- `ai-service/app/llm/circuit_breaker.py` — circuit breaker (use `pybreaker`); open circuit → fallback explanation routed to IT_SUPPORT
- `ai-service/app/prompts/` — real system prompts: ExplainerNode (plain-English explanation) + RouterNode (`with_structured_output(AIRouting)`, Team enum + confidence)
- `rsm-spt/` — extends `BaseWorker`, simulates pricing/stock check, logs WARN/ERROR to ES on failure
- Complete `validator-service/` — real validation rules, error cases that produce WARN/ERROR logs
- `avalara/` → **handed to Person C** (morning task, simplest service, copy the rsm-spt pattern)
- Wire all microservices into `docker-compose.yml`
- Test: Order Engine → all 3 services consume and process → WARN/ERROR logs land in fixtures/ES

**Person C — Avalara + real-time dashboard:**
- Morning: `avalara/` microservice — extends `BaseWorker`, simulates tax calculation, copy the rsm-spt pattern (good exercise: proves the BaseWorker pattern is truly reusable)
- `frontend/src/hooks/use-websocket.ts` — WebSocket connection hook
- `frontend/src/lib/ws.ts` — WebSocket client with reconnection
- `frontend/src/components/live-feed.tsx` — real-time issue feed from WebSocket
- `frontend/src/app/issues/[id]/page.tsx` — issue detail page
- `frontend/src/components/journey-timeline.tsx` — visual timeline of order events
- Connect to Person A's WebSocket endpoint — test with real data

### Day 5 — Depth + Edge Cases

**Person A — Resilience:**
- `backend/app/messaging/dlq_handler.py` — monitor DLQs (both `issues.raw` and `issues.processed`), surface dead-lettered messages as issues in the dashboard
- `backend/app/journeys/es_client.py` — if real ES access arrived: implement the REST version behind the existing interface. If not: enrich the mock fixtures
- Error handling everywhere: what happens when RabbitMQ goes down? Redis unavailable? DB unreachable?
- Health check endpoint with dependency status

**Person B — AI Service hardening:**
- `ai-service/app/polling/elasticsearch.py` — if real ES access arrived: implement the REST poller behind the existing interface
- Dedup tuning: TTL on dedup keys (how long is a repeat "the same issue"? start with 1h), dedup metrics (hits vs misses logged)
- Circuit breaker tuning — test with LLM timeouts, failures
- Redis caching — hash similar issues, serve cached explanations without LLM calls
- Fallback behavior when circuit breaker is open (generic explanation, routed to IT_SUPPORT)
- Prompt engineering — refine system prompts for ExplainerNode and RouterNode
- Test with real issue scenarios: validation failure, tax calculation error, timeout, DLQ entry
- Add Order Engine: `POST /api/orders` endpoint that creates realistic order events

**Person C — Frontend polish:**
- Filters and search on issues list
- Journey timeline interactions — click to expand event details
- AI explanation display — formatted natural language + team assignment
- Notification log view (which notifications were sent, when, where)
- Loading states, error states, empty states for every page
- Responsive layout

---

## Day 6: Integration Day

**All three together again.** This is the most important day after Days 1–2.

Morning — end-to-end flow:
- `docker compose up` — all services running
- POST an order to Order Engine (use `make break-order` for one that fails validation)
- Watch it flow: Order Engine → RabbitMQ `orders.*` → Validator / RSM-SPT / Avalara → WARN/ERROR log lands in ES (or fixtures)
- AI Service poller picks it up in the next sliding window → publishes to `issues.raw`
- AI Service consumer: dedup MISS → ExplainerNode → RouterNode → publishes to `issues.processed`
- Send the same broken order again → dedup HIT → ACK and drop, no duplicate LLM call (verify in logs)
- Core Backend consumes `issues.processed`, writes to `order_issues`, pushes via WebSocket
- Frontend shows issue in real-time with AI explanation and team assignment
- Notification appears in Slack/Teams

Afternoon — fix what broke:
- Message format mismatches between services
- WebSocket connection issues
- Race conditions in journey assembly (events arriving out of order)
- Circuit breaker behavior with real LLM calls
- Test DLQ flow: kill a service mid-processing, see the message land in DLQ, surface it as an issue

End of Day 6 deliverable:
- [ ] Complete end-to-end flow works: order → events → journey → AI explanation → frontend
- [ ] WebSocket real-time updates working in frontend
- [ ] At least one notification channel working (Slack or Teams)
- [ ] DLQ monitoring surfaces failed messages

---

## Day 7: Testing + Hardening

**Split again, but focused on quality.**

**Person A:**
- Integration tests for Core Backend — test with real RabbitMQ (testcontainers or docker)
- Load test: 50 orders simultaneously — does journey assembly hold up?
- Verify `trace_id` propagation: grep logs across all services for one `trace_id`, confirm complete chain
- CI pipeline setup if time allows

**Person B:**
- AI Service tests with mocked LLM — test every failure mode
- Test circuit breaker: 5 consecutive failures → circuit opens → fallback response → circuit recovers
- Redis cache hit/miss scenarios
- Microservice resilience: restart a service, confirm it reconnects to RabbitMQ and resumes

**Person C:**
- Frontend error handling: backend down, WebSocket disconnects, malformed data
- Cross-browser check (Chrome, Firefox minimum)
- Accessibility basics: keyboard navigation, screen reader labels
- README with screenshots of the dashboard

---

## Day 8: Demo Prep + Final Fixes

**All together, morning standup to triage.**

Morning:
- Fix the top 3 bugs from Day 7 testing
- Freeze features — no new code after lunch
- Write/update `README.md` at root with: architecture diagram, setup instructions, demo script
- Each person writes a short section documenting their services
- Update `CLAUDE.md` with final architecture decisions

Afternoon:
- Rehearse the demo (20 min max):
  1. Show architecture diagram (2 min)
  2. Create an order, show it flowing through services in RabbitMQ UI (3 min)
  3. Show journey assembly in real-time on the dashboard (3 min)
  4. Trigger a failure — show AI explanation + team routing (3 min)
  5. Show Slack/Teams notification (2 min)
  6. Show DLQ handling (2 min)
  7. Show circuit breaker in action — kill LLM, show fallback (2 min)
  8. Q&A (3 min)
- Run the demo flow 3 times — make sure it's reliable
- Tag `v1.0.0`, merge to main

---

## Critical Path — What Blocks What

```
Day 1-2: shared schemas + base worker (BLOCKS EVERYTHING)
         │
         ├─→ Day 3: Core Backend consumer (issues.processed) + journey ──→ Day 4: WebSocket + notifications
         │                                                                        │
         ├─→ Day 3: AI ingestion pipeline (poll → issues.raw → dedup            │
         │          → fake graph → issues.processed) ──→ Day 4: real LLM + circuit breaker + microservices
         │                                                        │
         ├─→ Day 3: Frontend scaffold (mock data) ──→ Day 4: Avalara + connect to real backend
         │                                                    │
         └───────────────────────────────────────────→ Day 6: INTEGRATION (all together)
                                                              │
                                                       Day 7: Testing
                                                              │
                                                       Day 8: Demo
```

Note: the AI Service ingestion pipeline (Day 3, Person B) is the heart of the system —
if the fake-LLM loop works end of Day 3, swapping in the real LLM on Day 4 is low-risk.

If Day 1–2 foundations are solid, Days 3–5 parallelize cleanly. If they're shaky, you'll burn Days 3–5 fixing schemas and message formats instead of building features.

---

## Daily Rituals (from Ways of Working)

- **9:00** — Standup with mentors: done / doing / blocked
- **Every PR** — peer review required, tag `ai-first` when Claude assisted
- **Before merge** — tests pass, linter clean, `trace_id` propagated, schemas used
- **End of day** — push all branches, update board, no work stays local

## Elasticsearch Contingency

If ES access doesn't arrive:
- AI Service polls through an abstract interface (`ai-service/app/polling/base.py`); the mock (`mock_poller.py`) returns realistic WARN/ERROR events from `infra/fixtures/es_events.json`
- Core Backend's journey lookup (`backend/app/journeys/es_client.py`) uses the same pattern — mock reads the same fixtures, filtered by trace_id
- When real access arrives (REST API), implement `elasticsearch.py` behind each interface — no other code changes needed
- Bonus for the demo: the mock poller can be triggered on demand, which makes the demo deterministic instead of waiting for a polling window

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| No ES access for 8 days | AI Service can't poll real events | Mock poller interface + JSON fixtures in ai-service |
| LLM rate limits / Azure issues | AI Service blocked | Circuit breaker + cached fallback + mock LLM for dev |
| RabbitMQ message format drift | Integration Day chaos | Shared schemas enforced from Day 1, tested in CI |
| WebSocket complexity | Frontend delays | Start with polling REST, upgrade to WS when stable |
| Scope creep | Won't finish | Feature freeze Day 8 morning, cut scope not quality |
