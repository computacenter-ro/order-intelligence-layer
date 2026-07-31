# Azure Deployment Guide — absolute-beginner, step by step

This deploys your app to **Azure Container Apps**. Do it in **Azure Cloud Shell**
(the `>_` icon in the Azure portal → choose **Bash**). Cloud Shell is already
logged in, so you don't install anything.

**Legend:** 🔑 = a secret value · 🤝 = do this WITH your Azure colleague.

---

## 0. Words you'll meet (read once)

- **Resource group** — a folder that holds Azure resources. Yours already exists:
  `rg-internship-2026`.
- **Image** — a frozen copy of one of your services (code + everything it needs to
  run), built from a `Dockerfile`. Container Apps run *images*.
- **Azure Container Registry (ACR)** — a private storage for your images. Azure can
  only run images it can pull from a registry, so images must live here first.
- **Container Apps Environment** — a shared, private network that your services live
  in. Services in the same environment can talk to each other.
- **Container App** — ONE running service (e.g. the backend). You create one per
  service. It runs an image.
- **Job** — a container that runs once and stops (not a always-on service). You'll
  use jobs for the DB migration and for firing scenarios.
- **Ingress** — how a service is reached.
  - **external** = reachable from the internet (gets an `https://...` URL).
  - **internal** = reachable only by other services in the environment.
  - **none** = not reachable at all (a background worker that only makes outbound calls).
- **Secret** — a sensitive value (password, key) stored encrypted, referenced by name.
- **Managed service** — a database/cache Azure runs for you (Postgres, Redis) instead
  of you running it in a container.

**Your app = 5 services + 3 data services + 2 jobs:**

| Service | Ingress | Port | Runs |
|---|---|---|---|
| collector | internal | 9200 | in-memory log collector |
| mock-services | none | — | background workers |
| ai-service | internal | 8100 | AI pipeline + chatbot |
| backend | **external** | 8000 | API the browser calls |
| dashboard | **external** | 3000 | the website |
| migrate | Job (once) | — | creates DB tables |
| injector | Job (scheduled) | — | fires scenarios |

Data services (managed, created in Part A): **Postgres, Redis, RabbitMQ**.

---

## PART A — Create the Azure resources

You already have a file for this: **`azure_step2_create_resources.md`**. Run it
first (Key Vault, ACR, Postgres, Redis, RabbitMQ 🤝, Container Apps Environment).

By the end you must have written down these values (you'll paste them below):
- ACR login server (looks like `croildevweu01.azurecr.io`)
- `DATABASE_URL`, `REDIS_URL`, `RABBITMQ_URL`
- Key Vault name, Environment name (`cae-oil-dev-weu`)

Set these variables again at the top of your Cloud Shell (so the commands below
work):

```bash
export RG="rg-internship-2026"
export LOC="westeurope"
export ENV_NAME="cae-oil-dev-weu"
export ACR_NAME="croildevweu01"                 # your ACR name (no hyphens)
export ACR_LOGIN=$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)
echo "ACR login server: $ACR_LOGIN"
```

---

## PART B — Get your code into Cloud Shell and build the images

**Why:** Azure needs your images in ACR. `az acr build` builds them *in the cloud*
(you don't need Docker on your machine).

**B1. Bring your code into Cloud Shell.**
```bash
git clone <your-git-repo-url>
cd order-intelligence-layer          # the folder that has docker-compose.yml + Dockerfile
```
*(If your repo is private, Cloud Shell will ask for a token/login — 🤝 your colleague
if it's a Computacenter private repo.)*

**B2. Build the shared app image** (used by backend, ai-service, collector,
mock-services, migrate, injector — they're the same image, they just run different
commands):
```bash
az acr build --registry "$ACR_NAME" --image oil-app:latest .
```
- `az acr build` = "build a Docker image in the cloud and store it in my registry".
- `--registry` = which ACR to store it in.
- `--image oil-app:latest` = the name:tag to give the image.
- `.` = build using the `Dockerfile` in the current folder.

**B3. Build the dashboard image** (separate — it needs your private npm token 🤝):
```bash
az acr build --registry "$ACR_NAME" --image oil-dashboard:latest \
  --build-arg NPM_TOKEN="<your-npm-token>" \
  dashboard/
```
🤝 The exact way to pass `NPM_TOKEN` depends on your dashboard `Dockerfile`
(it may expect a build arg or a build secret). Do this line with your colleague so
the token isn't mishandled. If the build fails on the npm step, that's the reason.

**B4. Confirm the images exist:**
```bash
az acr repository list --name "$ACR_NAME" -o table
```
You should see `oil-app` and `oil-dashboard`.

**Get the registry login so Container Apps can pull the images:**
```bash
export ACR_USER=$(az acr credential show -n "$ACR_NAME" --query username -o tsv)
export ACR_PASS=$(az acr credential show -n "$ACR_NAME" --query 'passwords[0].value' -o tsv)
```

---

## PART C — Put your secrets somewhere the apps can read

**Simplest for a demo:** pass secrets straight into each Container App with
`--secrets` (shown in Part E). A secret named `db-url` is then referenced by an env
var as `secretref:db-url`. That's what the commands below use.

*(Org-preferred alternative 🤝: store them in Key Vault with
`az keyvault secret set --vault-name <kv> --name db-url --value "<value>"` and have
the apps reference Key Vault via a managed identity. More wiring — do it with your
colleague only if required.)*

Have these values ready (from Part A):
`DATABASE_URL`, `REDIS_URL`, `RABBITMQ_URL`, `JWT_SECRET` (make a long random one),
`AZURE_AI_FOUNDRY_API_KEY`, and the Foundry endpoint + deployment names.

Make a JWT secret now:
```bash
export JWT_SECRET=$(openssl rand -base64 48)
echo "$JWT_SECRET"     # save it somewhere
```

---

## PART D — Create the database tables (a one-time Job)

**Why:** your app expects tables to exist. This runs `alembic upgrade head` once.

```bash
az containerapp job create \
  --name "ca-oil-migrate-dev-weu" \
  --resource-group "$RG" \
  --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" \
  --registry-username "$ACR_USER" \
  --registry-password "$ACR_PASS" \
  --trigger-type Manual \
  --replica-timeout 600 \
  --secrets "db-url=<DATABASE_URL>" \
  --env-vars "DATABASE_URL=secretref:db-url" "DB_SSL=require" \
  --command "/bin/sh" "-c" "alembic upgrade head"

# now run it once:
az containerapp job start --name "ca-oil-migrate-dev-weu" --resource-group "$RG"
```
- `job create` = define a run-once container.
- `--trigger-type Manual` = it only runs when you tell it to.
- `--secrets` = define a secret named `db-url`.
- `--env-vars ... secretref:db-url` = expose that secret to the app as `DATABASE_URL`.
- `--command "/bin/sh" "-c" "..."` = the command to run. The `/bin/sh -c "..."`
  wrapper is the reliable way to pass a full command line.

Check it succeeded:
```bash
az containerapp job execution list --name "ca-oil-migrate-dev-weu" --resource-group "$RG" -o table
```
Look for `Succeeded`. If it failed, view logs (🤝 if the error is unclear).

---

## PART E — Deploy the 5 services

**Order matters:** create the internal ones first, capture each one's internal
address, and feed it to the next. A service's internal address is found with:
```bash
az containerapp show -n <app-name> -g "$RG" --query properties.configuration.ingress.fqdn -o tsv
```

### E1. collector (internal, port 9200)
```bash
az containerapp create \
  --name "ca-oil-collector-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi \
  --ingress internal --target-port 9200 \
  --command "/bin/sh" "-c" "uvicorn pipeline.mock_es.app:app --host 0.0.0.0 --port 9200"

# capture its address for the next services:
export COLLECTOR_FQDN=$(az containerapp show -n ca-oil-collector-dev-weu -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
export ES_URL="https://$COLLECTOR_FQDN"
echo "$ES_URL"
```
- `--min-replicas 1 --max-replicas 1` = always exactly one copy (never scale to zero,
  so its in-memory logs don't vanish).
- `--cpu / --memory` = size. 0.5 vCPU / 1 GiB is fine here.
- `--ingress internal` = only other services can reach it.

### E2. mock-services (no ingress — background workers)
```bash
az containerapp create \
  --name "ca-oil-mock-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi \
  --secrets "rabbit-url=<RABBITMQ_URL>" \
  --env-vars "ES_URL=$ES_URL" "RABBITMQ_URL=secretref:rabbit-url" \
  --command "/bin/sh" "-c" "python -m pipeline.services.run_all"
```
*(No `--ingress` flag = no inbound access; it only makes outbound calls.)*

### E3. ai-service (internal, port 8100)
```bash
az containerapp create \
  --name "ca-oil-ai-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --min-replicas 1 --max-replicas 1 --cpu 1.0 --memory 2.0Gi \
  --ingress internal --target-port 8100 \
  --secrets "redis-url=<REDIS_URL>" "rabbit-url=<RABBITMQ_URL>" "aoai-key=<AZURE_AI_FOUNDRY_API_KEY>" \
  --env-vars \
     "ES_URL=$ES_URL" \
     "REDIS_URL=secretref:redis-url" \
     "RABBITMQ_URL=secretref:rabbit-url" \
     "AZURE_AI_FOUNDRY_ENDPOINT=<your-endpoint>" \
     "AZURE_AI_FOUNDRY_API_KEY=secretref:aoai-key" \
     "AZURE_AI_FOUNDRY_DEPLOYMENT_EXPLAINER=<name>" \
     "AZURE_AI_FOUNDRY_DEPLOYMENT_ROUTER=<name>" \
     "AZURE_AI_FOUNDRY_DEPLOYMENT_SUMMARY=<name>" \
     "AZURE_AI_FOUNDRY_DEPLOYMENT_CHAT=<name>" \
  --command "/bin/sh" "-c" "python -m ai_service.main"

# capture its address for the backend:
export AI_FQDN=$(az containerapp show -n ca-oil-ai-dev-weu -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
export AI_SERVICE_URL="https://$AI_FQDN"
echo "$AI_SERVICE_URL"
```
*(I gave ai-service a bit more CPU/RAM because it loads the embedding model.)*

### E4. backend (EXTERNAL, port 8000)
The browser calls this, so `--ingress external`. It needs almost every value.
```bash
az containerapp create \
  --name "ca-oil-backend-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --min-replicas 1 --max-replicas 1 --cpu 0.75 --memory 1.5Gi \
  --ingress external --target-port 8000 \
  --secrets "db-url=<DATABASE_URL>" "rabbit-url=<RABBITMQ_URL>" "jwt=$JWT_SECRET" \
  --env-vars \
     "DATABASE_URL=secretref:db-url" "DB_SSL=require" \
     "RABBITMQ_URL=secretref:rabbit-url" \
     "AI_SERVICE_URL=$AI_SERVICE_URL" \
     "JWT_SECRET=secretref:jwt" \
     "AUTH_COOKIE_SECURE=true" "AUTH_COOKIE_SAMESITE=none" \
     "PASSWORD_LOGIN_ENABLED=true" \
     "ADMIN_USERNAME=admin" \
     "CORS_ALLOW_ORIGINS=__FILL_AFTER_DASHBOARD__" \
     "DASHBOARD_URL=__FILL_AFTER_DASHBOARD__" \
  --command "/bin/sh" "-c" "uvicorn backend.main:app --host 0.0.0.0 --port 8000"

# capture the backend's PUBLIC url:
export BACKEND_FQDN=$(az containerapp show -n ca-oil-backend-dev-weu -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
export BACKEND_URL="https://$BACKEND_FQDN"
echo "$BACKEND_URL"
```
⚠️ Two env values (`CORS_ALLOW_ORIGINS`, `DASHBOARD_URL`) point at the *dashboard*,
which doesn't exist yet. You'll set them in E6 after the dashboard has a URL.
🤝 Check the backend's real start command against `docker-compose.yml` — if it isn't
`uvicorn backend.main:app`, use the one from compose.

### E5. dashboard (EXTERNAL, port 3000)
```bash
az containerapp create \
  --name "ca-oil-dashboard-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-dashboard:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi \
  --ingress external --target-port 3000 \
  --env-vars \
     "NEXT_PUBLIC_API_URL=$BACKEND_URL" \
     "NEXT_PUBLIC_WS_URL=wss://$BACKEND_FQDN/ws"

export DASH_FQDN=$(az containerapp show -n ca-oil-dashboard-dev-weu -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
export DASHBOARD_URL="https://$DASH_FQDN"
echo "$DASHBOARD_URL"
```
⚠️ `NEXT_PUBLIC_*` are usually baked at **build** time for Next.js. If the dashboard
can't reach the backend, you may need to rebuild the dashboard image (B3) with these
two values as build args. 🤝 flag this with your colleague if the UI loads but calls fail.

### E6. Close the loop — tell the backend the dashboard's URL
Now that the dashboard URL exists, update the two placeholders:
```bash
az containerapp update \
  --name "ca-oil-backend-dev-weu" --resource-group "$RG" \
  --set-env-vars "CORS_ALLOW_ORIGINS=$DASHBOARD_URL" "DASHBOARD_URL=$DASHBOARD_URL"
```
- `az containerapp update --set-env-vars` = change env vars on an existing app and
  restart it. This is also the command your future CD pipeline would use.

**This step is what makes login work.** If `CORS_ALLOW_ORIGINS` / `DASHBOARD_URL` /
the SameSite=none + Secure cookie don't all match the real dashboard URL, the site
loads but login silently fails.

---

## PART F — Nightly database cleanup (pg_cron) 🤝

On the managed Postgres, enable the `pg_cron` extension (a server parameter your
colleague can toggle), then connect once and schedule the delete:
```sql
SELECT cron.schedule('nightly-prune', '0 3 * * *',
  $$DELETE FROM journey_events WHERE ts < now() - interval '5 days'$$);
SELECT cron.schedule('nightly-prune-alerts', '0 3 * * *',
  $$DELETE FROM alerts WHERE emitted_at < now() - interval '5 days'$$);
```
This keeps the database from filling up over the 14 days.

---

## PART G — Start the injector (a scheduled Job)

**Why:** this fires order scenarios so the dashboard shows live data.
```bash
az containerapp job create \
  --name "ca-oil-injector-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --trigger-type Schedule --cron-expression "*/1 * * * *" \
  --replica-timeout 120 \
  --secrets "rabbit-url=<RABBITMQ_URL>" \
  --env-vars "RABBITMQ_URL=secretref:rabbit-url" \
  --command "/bin/sh" "-c" "python -m pipeline.injector.inject --all"
```
- `--trigger-type Schedule --cron-expression "*/1 * * * *"` = run every minute.
- Adjust the cron / command to your preferred rate (e.g. fire a couple of scenarios
  per run). You can also just run it manually with `az containerapp job start` while
  demoing.

---

## PART H — Verify

```bash
echo "Open this in your browser: $DASHBOARD_URL"
```
1. The login page loads.
2. Log in (admin / your admin password, if password login is on).
3. Watch the Alert Feed — within a minute or two, alerts and journeys appear.

If the page loads but nothing works after login → it's almost always the
CORS/cookie/URL trio from E6. Double-check those three values equal the exact
dashboard URL.

---

## Teardown (when the 14 days are over)

You're in a SHARED resource group, so DON'T delete the whole group. Delete only what
you made:
```bash
az containerapp delete -n ca-oil-collector-dev-weu -g "$RG" --yes
az containerapp delete -n ca-oil-mock-dev-weu -g "$RG" --yes
az containerapp delete -n ca-oil-ai-dev-weu -g "$RG" --yes
az containerapp delete -n ca-oil-backend-dev-weu -g "$RG" --yes
az containerapp delete -n ca-oil-dashboard-dev-weu -g "$RG" --yes
az containerapp job delete -n ca-oil-migrate-dev-weu -g "$RG" --yes
az containerapp job delete -n ca-oil-injector-dev-weu -g "$RG" --yes
az containerapp env delete -n "$ENV_NAME" -g "$RG" --yes
az postgres flexible-server delete -n psql-oil-dev-weu-01 -g "$RG" --yes
az redis delete -n redis-oil-dev-weu-01 -g "$RG" --yes
az acr delete -n "$ACR_NAME" -g "$RG" --yes
az keyvault delete -n kv-oil-dev-weu-01 -g "$RG"
```
(Adjust names to what you actually created. Ask your colleague before deleting the
RabbitMQ resource, since that was their setup.)

---

## When you get stuck

- **A command flag is rejected** → CLI versions differ; run the command with `--help`
  (e.g. `az containerapp create --help`) or ask your colleague. Don't guess.
- **A container won't start** → view its logs:
  `az containerapp logs show -n <app-name> -g "$RG" --tail 100`
- **The `--command` override misbehaves** → the `/bin/sh -c "..."` form is the most
  reliable; if it still fights you, 🤝 your colleague.
- **Login fails on the deployed site** → re-check E6 (CORS_ALLOW_ORIGINS, DASHBOARD_URL,
  AUTH_COOKIE_SAMESITE=none, AUTH_COOKIE_SECURE=true) all equal the exact dashboard URL.

The two spots most likely to need your colleague: **B3** (dashboard image + npm token)
and **Part A's** managed services + RabbitMQ. Everything else you can drive yourself
by following the commands above in order.
