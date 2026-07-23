# Frontend Build Plan — IT Support Dashboard

Build order: easiest/most foundational first, advanced/integration last.
Each phase ends with something you can **see working**. Do one checkbox at a time.

Reference: `dashboard-wireframe.html` (visual truth) + `CLAUDE.md` (the data contract).
Stack assumed: Next.js (App Router) + TypeScript + Tailwind.

---

## Phase 0 — Foundation (no UI yet, but everything depends on this)

- [ ] **0.1 Scaffold the app.** `npx create-next-app@latest dashboard --typescript --tailwind --app`. Run it, see the default page.
- [ ] **0.2 Define the types.** Create `types.ts` with `LogLine`, `ProcessedAlert`, `Journey`, `JourneyEvent`, and the WS event shapes (`alert.new`, `journey.updated`, `journey.completed`). Copy field names *exactly* from CLAUDE.md. This is your contract — get it right once.
- [ ] **0.3 Create fixtures.** `fixtures.ts` with ~8 mock alerts and ~8 journeys (reuse the arrays from the wireframe). Typed against 0.2 so any drift shows as a compile error.
- [ ] **0.4 App shell.** Sidebar nav + main area, routes for `/` (feed), `/journeys`, `/overview`. Clicking nav switches pages. No data yet — just the skeleton and routing.

## Phase 1 — Static screens from fixtures (build every screen, no live data)

- [ ] **1.1 Alert card component.** Renders one `ProcessedAlert`: level chip, service, time, explanation. Props-driven.
- [ ] **1.2 AI vs fallback rendering.** Add the badge, department chip, and confidence bar. Make the `source:"fallback"` variant visibly degraded (dashed, grey, "unprocessed", routed to general). This is your most important visual rule — nail it early.
- [ ] **1.3 Alert Feed page.** Map fixtures → a list of cards. Newest first.
- [ ] **1.4 Journeys table page.** Render fixtures as rows with status pills (success / failed / timed-out / in-progress).
- [ ] **1.5 Journey detail page.** Outcome banner + AI summary + pipeline path (done/stop/skipped nodes) + event timeline with per-step AI notes. Build from one fixture journey.
- [ ] **1.6 Overview page.** Stat cards, department/failure bar breakdowns, volume sparkline, LLM-health banner. All from fixture numbers.
- [ ] **1.7 Alert detail drawer.** Click a card → slide-over with raw log JSON, ids, and a "view journey" link.

## Phase 2 — Interactivity (still local data)

- [ ] **2.1 Feed filters.** Department, source (AI/fallback), level. Filter the fixture list.
- [ ] **2.2 Search.** Filter by orderId / eventId / message text.
- [ ] **2.3 Cross-page navigation.** Journey row → journey detail; alert drawer "view journey" → the right journey detail.
- [ ] **2.4 States.** Loading skeletons, empty feed, empty journey list. Build these now so they're not an afterthought.

## Phase 3 — Real data layer (swap fixtures for a fake backend)

- [ ] **3.1 REST client.** A `lib/api.ts` that calls `GET /alerts`, `GET /journeys`, `GET /journeys/{id}` against a base URL from an env var. Point it at your mock for now.
- [ ] **3.2 Mock WS/REST server.** A small script that serves those endpoints and replays the 10 scenarios over WebSocket on a timer (alerts, then journey chunks, then completion). *(Ask me to generate this.)*
- [ ] **3.3 WebSocket hook.** `useWebSocket` that connects to `/ws` and dispatches `alert.new` / `journey.updated` / `journey.completed`.
- [ ] **3.4 Live feed.** New `alert.new` events prepend to the feed. Add the "N new" pill when scrolled down.
- [ ] **3.5 Connection status.** Sidebar indicator: live / reconnecting / disconnected, with auto-reconnect.

## Phase 4 — The hard/advanced parts (the stuff that makes it real)

- [ ] **4.1 Progressive journey assembly.** `journey.updated` appends events to an in-progress journey; show the "still assembling…" state; `journey.completed` fills in outcome + summary.
- [ ] **4.2 Late-arriving journeys.** An alert can reference a journey that doesn't exist in the UI yet — the "view journey" link must resolve gracefully once it arrives.
- [ ] **4.3 Client-side dedup.** Both queues are at-least-once, so drop duplicate `alert_id` / `log_id`. Never render the same alert twice.
- [ ] **4.4 Degraded-LLM behavior.** Detect a spike of `source:"fallback"` alerts and surface the LLM-health banner + breaker state on Overview, driven by real data.
- [ ] **4.5 Live Overview metrics.** Compute the counts/breakdowns from the actual alert/journey stream instead of fixtures.
- [ ] **4.6 Polish.** Responsive layout, keyboard/focus for the drawer, error boundaries, light/dark if wanted.

## Phase 5 — Integration with the real system

- [ ] **5.1 Point at the real backend.** Change the base URL / WS URL to the FastAPI backend on `:8000`. Fix any CORS.
- [ ] **5.2 Verify against the contract.** Confirm real JSON matches your types from 0.2; reconcile any drift *with your teammates*, not by quietly changing the UI.
- [ ] **5.3 End-to-end.** Run `injector --all`, watch 10 journeys flow through with correct outcomes, alerts on the feed, journeys completing with summaries.

---

### Suggested rhythm
Phases 0–2 you can do **entirely solo, today**, with zero dependency on your teammates.
Phase 3 needs the mock server (small, self-contained).
Phase 5 is the only part that needs the real backend + AI service to be ready.

### Definition of "done" for each item
It renders, it's typed against the contract, and its empty/loading/error state exists. Don't move on until those three hold.