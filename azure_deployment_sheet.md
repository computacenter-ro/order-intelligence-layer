# Azure Container Apps — Deployment Sheet

Hand this to your Azure colleague. It maps every piece of the app to its Azure
form, with the exact ports, ingress, and environment variables each one needs.

**Legend:** 🌐 external ingress (internet-facing) · 🔒 internal ingress (reachable
only inside the environment) · ⚙️ no ingress (worker/job, makes outbound calls
only) · 🤝 = decision/step to do together.

---

## 1. What runs where

| Piece | Azure form | Notes |
|---|---|---|
| **postgres** | 🤝 **Azure Database for PostgreSQL – Flexible Server** (managed) | Holds journeys/alerts — the only durable data. Enable **`pg_cron`** for the nightly prune. |
| **redis** | 🤝 **Azure Cache for Redis** (managed) | Caches only (dedup, semcache, RAG index) — rebuildable. Uses `rediss://` (TLS, port 6380) + password. |
| **rabbitmq** | 🤝 **RabbitMQ resource** | No first-class managed RabbitMQ on Azure — colleague decides: container app (`rabbitmq:3.13-management`), Marketplace (CloudAMQP), or a VM. Uses `amqps://` if managed/TLS. |
| **migrate** | **Container Apps Job** (run once) | `alembic upgrade head`. Run AFTER Postgres exists, BEFORE the app services. |
| **collector** | **Container App** 🔒 | Port **9200**. In-memory log collector. |
| **mock-services** | **Container App** ⚙️ | No inbound port — it's a worker (baton consumers). Reaches collector + RabbitMQ. |
| **ai-service** | **Container App** 🔒 | Port **8100**. Poller + LangGraph + semantic cache + RAG index. |
| **backend** | **Container App** 🌐 | Port **8000**. The browser calls it directly, so it needs external ingress. |
| **dashboard** | **Container App** 🌐 | Port **3000**. Next.js UI. Build needs **`NPM_TOKEN`** 🤝. |
| **injector** | **Container Apps Job** (scheduled) | `python -m pipeline.injector.inject` — fires scenarios. Schedule at your ~2-per-60s cadence. |

All app services use the **same image** (`oil-app`, built from the repo Dockerfile)
except the dashboard, which has its own build.

---

## 2. Per-service environment variables

Fill the `<...>` placeholders with the real Azure resource endpoints once created.
On ACA, services address each other by their **internal URL** (colleague will give
you the exact form, e.g. `https://collector.internal.<env-domain>`).

### migrate (Job)
| Var | Value |
|---|---|
| `DATABASE_URL` | 🔑 managed Postgres connection string (asyncpg) |
| `DB_SSL` | `require` |

### collector 🔒 (port 9200)
| Var | Value |
|---|---|
| `MOCK_ES_MAX_LOGS` | `200000` (default is fine) |
*(No infra dependencies — it just receives POSTs.)*

### mock-services ⚙️
| Var | Value |
|---|---|
| `ES_URL` | `<collector internal URL>` (e.g. `http://collector...:9200`) |
| `RABBITMQ_URL` | 🔑 `amqps://<rabbitmq>` |

### ai-service 🔒 (port 8100)
| Var | Value |
|---|---|
| `ES_URL` | `<collector internal URL>` |
| `REDIS_URL` | 🔑 `rediss://<redis>` |
| `RABBITMQ_URL` | 🔑 `amqps://<rabbitmq>` |
| `AZURE_AI_FOUNDRY_ENDPOINT` | your Foundry endpoint |
| `AZURE_AI_FOUNDRY_API_KEY` | 🔑 secret |
| `AZURE_AI_FOUNDRY_DEPLOYMENT_EXPLAINER` / `_ROUTER` / `_SUMMARY` / `_CHAT` | deployment names |
| `SEMCACHE_*`, `RAGINDEX_*` | defaults fine (see CLAUDE.md) |
| `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` | 🔑 optional (LLM stats) |

### backend 🌐 (port 8000)
| Var | Value |
|---|---|
| `DATABASE_URL` | 🔑 managed Postgres (asyncpg) |
| `DB_SSL` | `require` |
| `RABBITMQ_URL` | 🔑 `amqps://<rabbitmq>` |
| `AI_SERVICE_URL` | `<ai-service internal URL>` (e.g. `http://ai-service...:8100`) |
| `JWT_SECRET` | 🔑 secret, ≥32 bytes |
| `JWT_TTL_SECONDS` | `28800` (8h, default) |
| `CORS_ALLOW_ORIGINS` | `https://<dashboard external URL>` |
| `AUTH_COOKIE_SECURE` | `true` |
| `AUTH_COOKIE_SAMESITE` | `none` |
| `DASHBOARD_URL` | `https://<dashboard external URL>` |
| `PASSWORD_LOGIN_ENABLED` | `false` if using Entra; else `true` + admin creds |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` | 🔑 only if password login enabled |
| `ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` / `ENTRA_CLIENT_SECRET` / `ENTRA_REDIRECT_URI` | 🔑 only if using Entra; redirect URI = `https://<backend URL>/auth/entra/callback` |
| `TEAMS_WEBHOOK_NETWORKING` / `_DEVOPS` / `_BACKEND` / `_DATABASE` / `_GENERAL` | 🔑 optional |

### dashboard 🌐 (port 3000)
| Var | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://<backend external URL>` (build + run) |
| `NEXT_PUBLIC_WS_URL` | `wss://<backend external URL>/ws` |
| `NPM_TOKEN` | 🔑 build-time only (private style-guide package) 🤝 |

### injector (Job, scheduled)
| Var | Value |
|---|---|
| `RABBITMQ_URL` | 🔑 `amqps://<rabbitmq>` |
| command | `python -m pipeline.injector.inject --mode continuous --interval 30` (≈2 per 60s) |

---

## 3. Secrets → Key Vault (🔑 rows above)

Put these in Key Vault; the container apps reference them (never paste raw):
`DATABASE_URL`, `REDIS_URL`, `RABBITMQ_URL`, `JWT_SECRET`, `AZURE_AI_FOUNDRY_API_KEY`,
`TEAMS_WEBHOOK_*`, `ADMIN_PASSWORD_HASH`, `ENTRA_CLIENT_SECRET`, `LANGSMITH_API_KEY`,
`NPM_TOKEN`.

Everything else (URLs, ports, flags like `DB_SSL`, `AUTH_COOKIE_*`,
`CORS_ALLOW_ORIGINS`) is plain configuration, not secret.

---

## 4. The URL cross-reference (who needs whose address)

Deploy the internal services first so their URLs exist before you wire the others:

```
collector  → needed by: mock-services (ES_URL), ai-service (ES_URL)
ai-service → needed by: backend (AI_SERVICE_URL)
backend    → needed by: dashboard (NEXT_PUBLIC_API_URL / WS), and its own
             CORS_ALLOW_ORIGINS / DASHBOARD_URL point BACK at the dashboard
dashboard  → its external URL is what backend's CORS + cookie config must match
```

Suggested deploy order: **Postgres/Redis/RabbitMQ → migrate job → collector →
mock-services → ai-service → backend → dashboard → injector job.**

---

## 5. Gotchas (already handled in code, but verify the values)

- **Login only works if the cookie trio is right:** `AUTH_COOKIE_SAMESITE=none` +
  `AUTH_COOKIE_SECURE=true` + `CORS_ALLOW_ORIGINS` = the dashboard's exact HTTPS
  origin. If any is wrong, the dashboard loads but login/API calls silently 401.
- **Postgres SSL:** the `?sslmode=require` form Azure gives you is handled (the code
  translates it for asyncpg), but keep `DB_SSL=require` set.
- **Redis/RabbitMQ TLS:** use `rediss://` and `amqps://` URLs (with the password
  Azure generates).
- **Entra redirect URI** must match `https://<backend URL>/auth/entra/callback`
  byte-for-byte, and be registered in the Azure app registration.
- **Stateful "no scale to zero":** if any of Postgres/Redis/RabbitMQ ends up as a
  container app rather than managed, set its **min replicas = 1** or a restart wipes
  it.
- **Nightly prune:** enable `pg_cron` on the Flexible Server and schedule
  `DELETE FROM journey_events WHERE ts < now() - interval '5 days';` (and the same
  for `alerts` on `emitted_at`) at 03:00.
