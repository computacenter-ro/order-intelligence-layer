# System-Documentation RAG — Plan

Adding a second knowledge source to the existing chatbot so it can answer questions
about **how the order-engine system works**, not only about **what has failed**.

Status: design agreed. Corpus split done (§11 step 1); no code yet. Open questions in §10.

---

## 1. Goal

Today the chatbot answers from runtime records — alerts, journeys, incidents. It can
tell you *this order failed at the margin check*. It cannot tell you *what the margin
check is, who owns it, or whether a margin block is even a fault*.

The new feature adds a second grounding channel: a corpus of per-service documentation
describing the real Computacenter order pipeline.

The two channels answer different questions and must not compete:

| | Incident channel (exists) | Docs channel (new) |
|---|---|---|
| Contains | things that happened | how things work |
| Source | Postgres (alerts, journeys) | markdown files in git |
| Grows | continuously, forever | when someone edits a file |
| Question | "why did ORD-6672 fail?" | "what does the Checker service do?" |

---

## 2. What exists today

**AI service (`:8100`)**

- `semcache.py` — owns the encoder (`all-MiniLM-L6-v2`, CPU, loaded once). Already
  shared by three consumers.
- `ragindex.py` — the incident index. In-memory dict → Redis `ai:ragindex`.
  Cap `RAGINDEX_MAX_ENTRIES` (5000), eviction oldest-first, `min_score` 0.30, k=5,
  feedback-blended ranking.
- `api.py` — `POST /index`, `POST /chat`, `GET /ragindex/stats`.
- `nodes.py` — `_CHAT_SYSTEM`, `compose_chat_answer`, `build_retrieval_answer`.
- `breaker.py` — circuit breaker shared by every LLM call.

**Backend (`:8000`)**

- `rag_client.py` — fire-and-forget push to `/index`, ask via `/chat`.
- `api.py::_context_text(session, kind, record_id, tz)` — the "anchor": exact SQL for
  the record the user clicked, flattened into the question text.
- `feedback.py` — thumbs up/down → ranking boosts, sent with each `/chat` request.
- `scripts/backfill_rag.py` — re-index everything from Postgres (idempotent).

**Dashboard (`:3000`)** — `ChatPanel.tsx`, scope set by the button clicked, not the page.

**Known live bugs this work depends on** (from the working notes):

- §6.4 — the AI service refuses to compose an answer when retrieval returns zero
  sources. A self-grounded question (anchor, or a docs-only answer) is silently
  discarded. **Blocks everything below.**
- §6.1 — everything in the prompt is labelled `incident records:`, which already
  caused the model to count alerts and report them as incidents.

---

## 3. Decisions, with reasoning

**D1 — A separate index, not a new `kind` in `ragindex`.**
Four reasons, the first decisive:
1. Eviction is oldest-first *by design*. Docs load once at startup, so they are
   permanently the oldest entries — silently evicted within days, with no error.
2. One shared `k=5` means doc chunks and alerts compete. "What does the checker do?"
   loses its own documentation to five checker *failures*.
3. `min_score` 0.30 is a recall floor tuned for incident history.
4. Feedback boosts would let a downvote on a badly-worded answer demote correct
   reference material.
Separating also lets us *guarantee* a mix (e.g. 3 doc chunks + 3 incident records)
instead of hoping the top-5 contains both.

**D2 — One chatbot, not two.** The highest-value questions need both channels at once
("SPT timed out — what does SPT do and what does its failure block?"). A router or a
mode toggle forces an exclusive choice that is wrong for exactly those questions.

**D3 — The bot speaks about the real system.** With a mock→real mapping applied
inward only, and never silently (see §7).

**D4 — Docs live in git; the index is built at container startup; no database.**
The files are the source of truth. The index is derived and disposable.

**D5 — A chunk is a section.** Large sections split further; small ones stay whole.

**D6 — Filter first, then similarity.** `service` + `kind` + `audience` narrow the
candidates; cosine ranks what survives. This is the shape `retrieve()` already has.

**D7 — Routing is a fast path, not a gate.** If no rule matches, fall back to plain
similarity over the whole docs index. You lose precision, never the answer. A bot that
says "I only handle these six question types" is the failure mode that kills adoption.

**D8 — Store everything; filter by audience at retrieval.** Storage is free (no cap,
no eviction). Dropping content at index time is irreversible; a filter is one line.

**D9 — `answering-policy.md` is the system prompt, never an indexed chunk.**
It is answering policy, not knowledge. If indexed it would be retrieved and cited.
**Done:** it was Part 1 of `chatbot-context.md` and is now its own file, so this is
enforced by structure rather than by a rule in the loader. It is the only file in the
corpus with no `yaml` frontmatter block — which is the loader's test for "not a service
doc", in preference to a hardcoded filename.

**D10 — §7 (log anatomy) is not indexed.** It is engineer-level *and* describes a
plain-text logback format the simulation never emits.

---

## 4. The corpus

Six files, ~2,600 lines: **five service docs plus `answering-policy.md`** (the system
prompt, not part of the corpus — see D9). The five share a strict template, which is what
makes mechanical chunking possible.

| Service | File | Notes |
|---|---|---|
| order-engine | `order-engine.md` | the hub; largest file; §12 escalation still `TODO`; one extra frontmatter key (`logback_context_name`) |
| inbound-order | `inbound-order.md` | front door; queue-driven |
| order-validator | `order-validator-web-service.md` | stateless; 26 log statements |
| rsm-ws | `rsm-ws.md` | read-only; has §13 provenance |
| jam-ws | `jam-ws.md` | smallest; 6 log statements; has §13 provenance |

**Not documented (8):** SPT, Checker, SOLR, Settings, Avalara, Track & Trace,
Outbound OSW, SAP/BTP + Salesforce. Checker and Avalara exist only as second-hand
accounts inside the Order Engine file.

### The template

```
YAML frontmatter: service, aliases, repo, log_files, log_format, host_retention,
                  environments, correlation_id, primary_join_key, statuses_written
§1  What this service does        §1.1 What it is NOT responsible for
§2  Key concepts                  §2.1 Concepts this document does not cover
§3  How work flows                §3.1 Why a step gets skipped
§4  The rules that stop something
§5  What users can do
§6  Statuses, and who writes them
§7  Log anatomy / levels / retries / retention
§8  Identifiers in the logs
§9  Journey stages + §9.1 Lines that look like errors but are not + §9.2 Queue topology
§10 Evidence that is not in the log file
§11 Blind spots and traps
§12 Escalation
§13 Provenance and known unknowns    (jam-ws and rsm-ws only)
```

The frontmatter is already a service registry — ten identical keys across every file.
It is parsed into a plain dict, no embeddings.

### Template changes needed

1. **Big tables → repeated small blocks.** A table row torn from its header is
   meaningless; keeping the table whole makes one huge chunk. Applies to §9, §4, §12.
2. **Tag each section** with its `kind` and `audience`.
3. **Add the mock `app_name` to each `aliases` list.**
4. **Finding first, code reason separate.** "You can't trust the EventId" is support-level;
   "there is no `MDC.remove` in a `finally`" is engineer-level. Same paragraph today.
5. **Fix Order Engine's sub-numbering drift** (its §9.2 is Regenerate; everyone else's
   is queue topology). Or simply match on heading text, never number.

---

## 5. Chunking

A chunk is a dict:

```python
{
  "id":       "jam-ws#1",
  "service":  "jam-ws",
  "aliases":  ["JAM", "JAM 2.0", "cc-jam-service"],
  "kind":     "what_it_does",
  "audience": "support",
  "text":     "[jam-ws · What this service does]\njam-ws answers one question ...",
  "vector":   [...]            # 384 floats
}
```

**Loader, in order:**

1. Read the YAML frontmatter → service name + aliases, applied to every chunk in the file.
2. Split the body on `##` headings → one piece per section.
3. Split long pieces again on sub-headings (`4.1`, `4.2`…) or table rows.
4. Prepend `[service · heading]` so the chunk carries its own identity.
5. Tag `kind` (lookup on heading text) and `audience`.
6. Embed. Append.

~60 lines total, most of it splitting.

**Section kinds**

| kind | Section | Answers |
|---|---|---|
| `what_it_does` | §1, §1.1 | "What does JAM do?" "Does OE assign the SAP number?" |
| `concept` | §2 | "What is a cart header?" "Blocking reason vs `ZM`?" |
| `why_skipped` | §3.1 | "It didn't complain about X — why?" |
| `rule` | §4 | "Why was my order blocked?" "Why wasn't it saved?" |
| `journey_stage` | §9 | "Where did it get to?" "Did it reach SAP?" |
| `not_an_error` | §9.1 | "15 `Not implemented` warnings — is that bad?" |
| `identifiers` | §8 | "I have an order number — what can I search on?" |
| `evidence_outside_logs` | §10 | "The log says nothing. Now what?" |
| `trap` | §11 | "Can I trust this EventId?" "200 means it worked, right?" |
| `escalation` | §12 | "Who do I raise this with? Is it even a fault?" |
| *(not indexed)* | §7, §13 | engineer-only / about the document |

Rough volume: Order Engine ≈ 33 chunks; ~400–600 across all thirteen services.

---

## 6. Retrieval

```
question
  ├─ detect service name        (closed set of 13 + aliases — plain string match)
  ├─ detect pasted log message  (exact substring vs the message catalogue)
  ├─ detect question kind       (heading-kind mapping)
  │
  ├─ any match → filter chunks by those labels, then cosine among survivors
  └─ no match  → plain cosine over the whole docs index          ← D7
```

Worked example:

```
"can I trust the EventId on this line?"
  filter:     service=order-engine, kind=trap   → 10 chunks survive
  similarity: rank those 10                     → §11.1 wins
  prompt:     that chunk + the question
```

The filter does the coarse work; similarity does the fine work. Neither alone suffices —
filtering leaves ~10 candidates, and unfiltered similarity across 600 lets unrelated
chunks in.

**Message catalogue** — a separate exact-match lookup, *not* part of the vector index.
Every verbatim message in the docs, mapped to its chunk. Embeddings are poor at exact
strings; a substring match is perfect. Pasting a log line and getting the right page is
the single most valuable behaviour, and it needs no AI at all.

**Prompt structure** — three clearly labelled blocks, replacing today's single
`incident records:` heading:

```
[the record you are looking at]     ← the anchor, if scoped
[related incident history]          ← ragindex results
[system documentation]              ← docs index results
```

---

## 7. Real system vs simulation

The docs describe the real services; the logs come from mocks. The gaps fall into
three tiers:

| Tier | Example | Handling |
|---|---|---|
| Clean 1:1 | `cc-order-engine` ↔ `order-engine`; hosts | mapping file |
| Same concept, different shape | real `eventId` is a DB sequence (`4471903`); mock is `evt-<uuid>` | map the *concept*, never the value |
| Not mappable | log format; queue names; Validator as HTTP call vs pipeline stage | exclude (§7) or flag the chunk |

`knowledge/service-map.yaml`:

```yaml
- doc: order-engine
  mock_app_name: cc-order-engine
  mock_host: CCECMEWEBT001
  aliases: [oe, "Order Engine", orderengine]
- doc: rsm-ws
  mock_app_name: cc-rsm-service
  ...
```

**Rule: never translate silently.** If someone pastes a `cc-order-engine` line, the
answer names the correspondence before using it. Quiet substitution produces confidently
wrong answers.

Note the mock was clearly built from these docs — `"Get order by Order Number:ORD-6001"`
and the `"Not implemented"` WARNs appear verbatim in both.

---

## 8. Build and deploy

```
knowledge/*.md  (git)
   → docker build   : files copied into the image
   → container start: encoder loads, files read, chunks built, index in RAM (~10s)
```

- **No database, no Redis key, no persistence.** Rebuilt from files in seconds, so
  storing it would create a second copy that can drift.
- **Docs must be in git.** Not in the repo → not in the image → empty index in
  production. (`docs/` is currently gitignored; use a new tracked folder.)
- **Changing a doc is a deploy.** Same pipeline as any code change, so documentation
  version always matches code version.
- **`POST /docs/reload`** — re-reads and rebuilds without a restart. Ten lines, behind auth.
- **Build in the background; self-disable on failure**, matching the semcache/ragindex
  pattern. Chatbot degrades to incident-only answers, never crashes.

Memory: ~600 chunks ≈ under 10 MB. The smallest in-memory store in the system
(semcache 500 entries; ragindex 5000; mock ES 200k log lines ≈ 90 MB).

---

## 9. Inventory

**Reuse unchanged**

| | Why |
|---|---|
| The encoder (`semcache.load_encoder`) | already shared by three consumers; docs become the fourth — no new ML dependency |
| `POST /chat` | same endpoint, same shape |
| `metadata` on records | deliberately untyped, so `service`/`kind`/`audience` need no AI-service change |
| `retrieve()`'s shape | already filters on metadata *before* cosine |
| Circuit breaker | unchanged |
| `ChatPanel.tsx` | same UI |
| PostgreSQL | nothing — no migration |

**New**

- second index instance (`ragindex.py` is the template — different cap, no eviction)
- startup loader (the one real piece of new code)
- service registry: parsed frontmatter + `service-map.yaml`
- entity detection: service-name and message matchers (pure functions, no LLM)
- `knowledge/` folder in git
- `POST /docs/reload`

**Changed**

| File | Change |
|---|---|
| `ai_service/api.py` | accept a "self-grounded" flag so zero sources ≠ refuse — **do this first** |
| `ChatRequest` | that flag; ideally separate search query from prompt context |
| `nodes.py::_CHAT_SYSTEM` | merge `answering-policy.md`; three labelled blocks |
| `backend/api.py::_context_text` | inject the failing service's doc when scoped |
| the `.md` files | template changes (§4) |

**Not needed:** new database · vector database · Postgres migration · new container ·
backfill script for docs.

**One architectural note.** Everything indexed today is derived from Postgres — the
backend owns the DB and pushes records up. Documentation was never in the database, so
the AI service reads the folder directly. A third, simpler path alongside the existing
one: *files own themselves*.

---

## 10. Open questions

Ordered by how much they change the implementation.

**1. Prompt and slot budget.** How many doc chunks vs incident records vs anchor text
per answer? `answering-policy.md` alone is ~200 lines and there are now three context
blocks. Needs a number before coding, because it decides `k` for each index.

**2. Should incident retrieval run at all on an explanatory question?** The working
notes already concluded retrieval adds little under an anchor and can contaminate
membership. "What does JAM do?" arguably needs zero incident records. Skipping frees
slots and removes a wrong-answer path — but needs the §6.4 fix first.

**3. Does feedback apply to doc chunks?** A downvote on a badly-worded answer would
demote a correct reference page. Recommendation: exclude docs from the feedback blend,
but it should be a deliberate choice.

**4. How do we know it works?** No evaluation set has been discussed, in a project with
1000+ tests. Proposal: a fixed list of ~30 questions with the expected chunk id for
each, asserted in CI. Cheap, and it catches template/chunking regressions immediately.

**5. How are doc sources rendered in the UI?** Today sources are alerts and journeys.
A doc source is different — `order-engine · §4.3 Margin check` — and should probably
not look like an incident. Small, but currently unspecified.

**6. Is `audience` fixed or switchable?** Fixed to `support` is simplest and makes
engineer chunks harmless dead weight. A toggle is more useful but is UI work.

**7. Placeholder files for the 8 undocumented services?** A stub carrying only
frontmatter + "not yet documented" makes the *"I have no documented knowledge of that
service"* answer deterministic rather than a fallback. Cheap; worth deciding.

**8. Language.** The docs quote DE/FR/NL message bundles. Does the assistant answer in
English only?

**9. `_TEMPLATE-new-service.md`** — referenced by two files but not yet seen. Needed
before restructuring, since it is the schema everything else follows.

---

## 11. Order of work

**No code required**

1. ~~Split `chatbot-context.md` into `answering-policy.md` + `order-engine.md`~~ **done** —
   the corpus is now six structurally uniform files (see §4).
2. ~~Move the corpus into a tracked folder~~ **done** — it lives in
   **`ai_service/knowledge/`**, inside the subsystem that reads it, matching the
   `pipeline/data/` precedent for subsystem-owned data. Two build properties come free
   there and are the reason for the location: `build-images.yml` already filters on
   `ai_service/**`, so a docs-only edit rebuilds the image; and `.dockerignore` excludes
   only `docs/` and `ways-of-working/`, so nothing here is stripped from the build context.
   Do not move it to a name inside that "Docs / editor noise" block, or the corpus silently
   stops reaching the image.
3. Settle the template (§4) — highest leverage, so the remaining eight files are written
   in the right shape rather than retrofitted.
4. ~~Write `service-map.yaml`~~ **done** — `ai_service/knowledge/service-map.yaml`.
   Three sections rather than one list, because the corpus has three cases: `services`
   (the 10 app_names that appear in logs; 5 mapped to a doc, 5 `doc: null`), `folded_in`
   (SOLR and Avalara have **no app_name of their own** — SOLR is emitted inside
   `cc-order-engine`, Avalara by `cc-validator-service`, so "show me the SOLR logs" has no
   answer and that is itself the finding), and `not_simulated` (SAP/BTP, Salesforce —
   answerable from docs, never from logs). Aliases are carried here **only** for entries
   with no doc file; a documented service's aliases stay in its own frontmatter so the two
   cannot drift.

**Code**

5. Fix §6.4 — zero sources must not mean refuse. Blocks the rest.
6. Loader + second index. Chunk ids are **heading-text slugs**
   (`order-engine#margin-check`), never ordinals — the template work in step 3 renumbers
   sections, which would silently reassign every ordinal id and invalidate the step 9
   evaluation set along with any citation already shown to a user.
7. Merge the prompt; three labelled blocks; rename away from "incident records".
8. Entity detection + kind routing, with the plain-similarity fallback. The message
   catalogue matches on **templates, not exact substrings**: documented messages contain
   `{}` placeholders (`Submitted order:{}`), so a pasted line never matches verbatim —
   split on `{}` and require the fixed segments in order.
9. Evaluation set in CI. Worth writing **before** step 6, not after: it is the only way to
   tell whether a template change improved retrieval or merely moved it.

**Smallest thing that proves it works:** chunk `jam-ws.md` by hand (it is the shortest),
load it, ask *"what does JAM do?"*, and confirm the answer comes from the doc and not
from an alert. Everything after that is repetition.