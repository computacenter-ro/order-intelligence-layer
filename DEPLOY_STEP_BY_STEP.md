# Deploy to Azure — one complete step-by-step guide

Everything is in THIS file. You run almost all of it in **Azure Cloud Shell**
(the `>_` icon in the Azure portal → **Bash**). Two things are done on your **own
laptop** (building the dashboard image) — clearly marked.

**Legend:** 🔑 a secret value · 🤝 do WITH your Azure colleague · 💻 run on YOUR laptop
(not Cloud Shell).

Important idea to keep in mind: your app reads its settings from **environment
variables**. The same code ran locally with `localhost` databases; on Azure you feed
it the **Azure** databases instead. You are NOT reusing your local dev URLs — you
build brand-new ones from the Azure resources you create below.

---

## STEP 0 — Open Cloud Shell and set your names

Open Cloud Shell (Bash), then paste this (edit the `01` suffix if a name is taken):

```bash
export RG="rg-internship-2026"        # your EXISTING resource group
export LOC="westeurope"
export ENV_NAME="cae-oil-dev-weu"

export KV_NAME="kv-oil-dev-weu-01"
export ACR_NAME="croildevweu01"       # no hyphens allowed for registries
export PG_NAME="psql-oil-dev-weu-01"
export REDIS_NAME="redis-oil-dev-weu-01"
export MI_NAME="id-oil-dev-weu"        # one identity all apps will share

export PG_ADMIN="oiladmin"
export PG_PASSWORD="ChangeMe-Str0ng!"  # 🔑 choose a strong password; REMEMBER it

# confirm you're on the right subscription:
az account show --query name -o tsv
```

---

## STEP 1 — Create the Azure resources

### 1a. Key Vault (stores your secrets)
```bash
az keyvault create --resource-group "$RG" --name "$KV_NAME" --location "$LOC"
```

### 1b. Container Registry (stores your images)
```bash
az acr create --resource-group "$RG" --name "$ACR_NAME" --sku Basic --admin-enabled true
export ACR_LOGIN=$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)
echo "ACR login server: $ACR_LOGIN"
```

### 1c. 🤝 PostgreSQL (your durable database)
```bash
az postgres flexible-server create \
  --resource-group "$RG" --name "$PG_NAME" --location "$LOC" \
  --admin-user "$PG_ADMIN" --admin-password "$PG_PASSWORD" \
  --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
  --public-access 0.0.0.0

az postgres flexible-server db create \
  --resource-group "$RG" --server-name "$PG_NAME" --database-name oil
```

### 1d. 🤝 Redis (slow: ~15-20 min — start it, keep going)
```bash
az redis create \
  --resource-group "$RG" --name "$REDIS_NAME" --location "$LOC" \
  --sku Basic --vm-size c0
```

### 1e. 🤝 RabbitMQ — NO managed Azure service exists
Decide with your colleague (a container, CloudAMQP, etc.). You just need the final
connection string, e.g. `amqps://user:pass@host:5671/`. **Ask them for it.**

### 1f. Container Apps Environment (the private network your services live in)
```bash
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az containerapp env create --resource-group "$RG" --name "$ENV_NAME" --location "$LOC"
```

---

## STEP 2 — Build your connection strings (from the AZURE resources)

You **assemble** these — they're not sitting anywhere ready-made.

```bash
# Postgres host (looked up), then the full URL:
export PG_HOST=$(az postgres flexible-server show -g "$RG" -n "$PG_NAME" --query fullyQualifiedDomainName -o tsv)
export DATABASE_URL="postgresql+asyncpg://$PG_ADMIN:$PG_PASSWORD@$PG_HOST:5432/oil?sslmode=require"

# Redis key (fetched), then the full URL (rediss:// = TLS, port 6380):
export REDIS_KEY=$(az redis list-keys -g "$RG" -n "$REDIS_NAME" --query primaryKey -o tsv)
export REDIS_URL="rediss://:$REDIS_KEY@$REDIS_NAME.redis.cache.windows.net:6380/0"

# JWT secret — you INVENT this (used to sign login cookies). Keep it constant.
export JWT_SECRET=$(openssl rand -base64 48)

# RabbitMQ — paste the value your colleague gave you (1e):
export RABBITMQ_URL="amqps://<user>:<pass>@<host>:5671/"

# Azure AI Foundry — you already have these:
export AOAI_ENDPOINT="<your foundry endpoint>"
export AOAI_KEY="<your foundry key>"
export AOAI_EXPLAINER="<deployment name>"
export AOAI_ROUTER="<deployment name>"
export AOAI_SUMMARY="<deployment name>"
export AOAI_CHAT="<deployment name>"

# quick check (don't share these — they contain passwords):
echo "$DATABASE_URL"; echo "$REDIS_URL"
```

---

## STEP 3 — Store the secrets in Key Vault

```bash
az keyvault secret set --vault-name "$KV_NAME" --name database-url --value "$DATABASE_URL"
az keyvault secret set --vault-name "$KV_NAME" --name redis-url    --value "$REDIS_URL"
az keyvault secret set --vault-name "$KV_NAME" --name rabbitmq-url --value "$RABBITMQ_URL"
az keyvault secret set --vault-name "$KV_NAME" --name jwt-secret   --value "$JWT_SECRET"
az keyvault secret set --vault-name "$KV_NAME" --name aoai-key     --value "$AOAI_KEY"
```

### 3b. Create one identity all apps use, and let it read the vault
```bash
az identity create -g "$RG" -n "$MI_NAME"
export MI_ID=$(az identity show -g "$RG" -n "$MI_NAME" --query id -o tsv)
export MI_PRINCIPAL=$(az identity show -g "$RG" -n "$MI_NAME" --query principalId -o tsv)

# 🤝 grant it "get" on the vault (needs permission to change the vault):
az keyvault set-policy -n "$KV_NAME" --object-id "$MI_PRINCIPAL" --secret-permissions get list
```
*(A "managed identity" is an automatic Azure login for your apps, so they can read
the vault without a password. If you're blocked from `set-policy`, ask your
colleague — or fall back to putting the raw values directly in each app's
`--secrets` instead of `keyvaultref`.)*

Helper variables for referencing vault secrets:
```bash
KVBASE="https://$KV_NAME.vault.azure.net/secrets"
export ACR_USER=$(az acr credential show -n "$ACR_NAME" --query username -o tsv)
export ACR_PASS=$(az acr credential show -n "$ACR_NAME" --query 'passwords[0].value' -o tsv)
```

---

## STEP 4 — Build the images

### 4a. The shared app image (Cloud Shell — no secret needed)
```bash
git clone <your-git-repo-url>
cd order-intelligence-layer          # folder with Dockerfile + docker-compose.yml
az acr build --registry "$ACR_NAME" --image oil-app:latest .
```

### 4b. 💻 The dashboard image — build on YOUR laptop (needs Docker + a GitHub token)
Your dashboard `Dockerfile` reads the npm token as a **BuildKit build secret**
(`--mount=type=secret,id=npm_token`), and it bakes the backend URL in at build time.
Cloud Shell has no Docker, so do this on your laptop where Docker is installed.

First you need the backend's public URL — but the backend isn't created yet. So the
order is: do STEP 5 + STEP 6 (create the backend) FIRST, note its URL, then come
back and run this. (I repeat this reminder in STEP 6.)

```bash
# on your laptop, in the repo root:
echo "<your GitHub Packages token>" > npm_token.txt     # 🔑 a GitHub PAT with read:packages 🤝

DOCKER_BUILDKIT=1 docker build \
  --secret id=npm_token,src=npm_token.txt \
  --build-arg NEXT_PUBLIC_API_URL="https://<BACKEND_URL>" \
  --build-arg NEXT_PUBLIC_WS_URL="wss://<BACKEND_URL>/ws" \
  -t "<ACR_LOGIN>/oil-dashboard:latest" \
  dashboard/

az acr login --name "<ACR_NAME>"
docker push "<ACR_LOGIN>/oil-dashboard:latest"
rm npm_token.txt
```
🤝 Get the GitHub Packages token from your colleague. Replace `<BACKEND_URL>`,
`<ACR_LOGIN>`, `<ACR_NAME>` with the real values.

---

## STEP 5 — Create the database tables (a one-time Job)

```bash
az containerapp job create \
  --name "ca-oil-migrate-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --user-assigned "$MI_ID" \
  --trigger-type Manual --replica-timeout 600 \
  --secrets "db-url=keyvaultref:$KVBASE/database-url,identityref:$MI_ID" \
  --env-vars "DATABASE_URL=secretref:db-url" "DB_SSL=require" \
  --command "/bin/sh" "-c" "alembic upgrade head"

az containerapp job start --name "ca-oil-migrate-dev-weu" --resource-group "$RG"
# check it says Succeeded:
az containerapp job execution list --name "ca-oil-migrate-dev-weu" --resource-group "$RG" -o table
```

---

## STEP 6 — Deploy the 5 services (in this order)

### 6a. collector (internal, port 9200)
```bash
az containerapp create \
  --name "ca-oil-collector-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi \
  --ingress internal --target-port 9200 \
  --command "/bin/sh" "-c" "uvicorn pipeline.mock_es.app:app --host 0.0.0.0 --port 9200"

export COLLECTOR_FQDN=$(az containerapp show -n ca-oil-collector-dev-weu -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
export ES_URL="https://$COLLECTOR_FQDN"
```

### 6b. mock-services (no ingress — background workers)
```bash
az containerapp create \
  --name "ca-oil-mock-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --user-assigned "$MI_ID" \
  --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi \
  --secrets "rabbit-url=keyvaultref:$KVBASE/rabbitmq-url,identityref:$MI_ID" \
  --env-vars "ES_URL=$ES_URL" "RABBITMQ_URL=secretref:rabbit-url" \
  --command "/bin/sh" "-c" "python -m pipeline.services.run_all"
```

### 6c. ai-service (internal, port 8100)
```bash
az containerapp create \
  --name "ca-oil-ai-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --user-assigned "$MI_ID" \
  --min-replicas 1 --max-replicas 1 --cpu 1.0 --memory 2.0Gi \
  --ingress internal --target-port 8100 \
  --secrets "redis-url=keyvaultref:$KVBASE/redis-url,identityref:$MI_ID" \
            "rabbit-url=keyvaultref:$KVBASE/rabbitmq-url,identityref:$MI_ID" \
            "aoai-key=keyvaultref:$KVBASE/aoai-key,identityref:$MI_ID" \
  --env-vars \
     "ES_URL=$ES_URL" "REDIS_URL=secretref:redis-url" "RABBITMQ_URL=secretref:rabbit-url" \
     "AZURE_AI_FOUNDRY_ENDPOINT=$AOAI_ENDPOINT" "AZURE_AI_FOUNDRY_API_KEY=secretref:aoai-key" \
     "AZURE_AI_FOUNDRY_DEPLOYMENT_EXPLAINER=$AOAI_EXPLAINER" \
     "AZURE_AI_FOUNDRY_DEPLOYMENT_ROUTER=$AOAI_ROUTER" \
     "AZURE_AI_FOUNDRY_DEPLOYMENT_SUMMARY=$AOAI_SUMMARY" \
     "AZURE_AI_FOUNDRY_DEPLOYMENT_CHAT=$AOAI_CHAT" \
  --command "/bin/sh" "-c" "python -m ai_service.main"

export AI_FQDN=$(az containerapp show -n ca-oil-ai-dev-weu -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
export AI_SERVICE_URL="https://$AI_FQDN"
```

### 6d. backend (EXTERNAL, port 8000)
```bash
az containerapp create \
  --name "ca-oil-backend-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --user-assigned "$MI_ID" \
  --min-replicas 1 --max-replicas 1 --cpu 0.75 --memory 1.5Gi \
  --ingress external --target-port 8000 \
  --secrets "db-url=keyvaultref:$KVBASE/database-url,identityref:$MI_ID" \
            "rabbit-url=keyvaultref:$KVBASE/rabbitmq-url,identityref:$MI_ID" \
            "jwt=keyvaultref:$KVBASE/jwt-secret,identityref:$MI_ID" \
  --env-vars \
     "DATABASE_URL=secretref:db-url" "DB_SSL=require" \
     "RABBITMQ_URL=secretref:rabbit-url" "AI_SERVICE_URL=$AI_SERVICE_URL" \
     "JWT_SECRET=secretref:jwt" \
     "AUTH_COOKIE_SECURE=true" "AUTH_COOKIE_SAMESITE=none" \
     "PASSWORD_LOGIN_ENABLED=true" "ADMIN_USERNAME=admin" \
     "CORS_ALLOW_ORIGINS=placeholder" "DASHBOARD_URL=placeholder" \
  --command "/bin/sh" "-c" "uvicorn backend.main:app --host 0.0.0.0 --port 8000"

export BACKEND_FQDN=$(az containerapp show -n ca-oil-backend-dev-weu -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
export BACKEND_URL="https://$BACKEND_FQDN"
echo "BACKEND URL: $BACKEND_URL"
```
👉 **Now build & push the dashboard image (STEP 4b on your laptop), using
`$BACKEND_URL` for the two `--build-arg` values.** Then continue:

### 6e. dashboard (EXTERNAL, port 3000)
```bash
az containerapp create \
  --name "ca-oil-dashboard-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-dashboard:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi \
  --ingress external --target-port 3000

export DASH_FQDN=$(az containerapp show -n ca-oil-dashboard-dev-weu -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
export DASHBOARD_URL="https://$DASH_FQDN"
echo "DASHBOARD URL: $DASHBOARD_URL"
```

### 6f. Tell the backend the dashboard's real URL (this makes LOGIN work)
```bash
az containerapp update \
  --name "ca-oil-backend-dev-weu" --resource-group "$RG" \
  --set-env-vars "CORS_ALLOW_ORIGINS=$DASHBOARD_URL" "DASHBOARD_URL=$DASHBOARD_URL"
```

---

## STEP 7 — Nightly database cleanup (pg_cron) 🤝

Ask your colleague to enable the `pg_cron` extension on the Postgres server, then
connect once (psql) and run:
```sql
SELECT cron.schedule('prune-events', '0 3 * * *',
  $$DELETE FROM journey_events WHERE ts < now() - interval '5 days'$$);
SELECT cron.schedule('prune-alerts', '0 3 * * *',
  $$DELETE FROM alerts WHERE emitted_at < now() - interval '5 days'$$);
```

---

## STEP 8 — Start firing scenarios (a scheduled Job)

```bash
az containerapp job create \
  --name "ca-oil-injector-dev-weu" \
  --resource-group "$RG" --environment "$ENV_NAME" \
  --image "$ACR_LOGIN/oil-app:latest" \
  --registry-server "$ACR_LOGIN" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --user-assigned "$MI_ID" \
  --trigger-type Schedule --cron-expression "*/1 * * * *" --replica-timeout 120 \
  --secrets "rabbit-url=keyvaultref:$KVBASE/rabbitmq-url,identityref:$MI_ID" \
  --env-vars "RABBITMQ_URL=secretref:rabbit-url" \
  --command "/bin/sh" "-c" "python -m pipeline.injector.inject --all"
```

---

## STEP 9 — Check it works

```bash
echo "Open in your browser: $DASHBOARD_URL"
```
Log in (admin + your admin password). Within a minute or two, alerts and journeys
should appear.

**If the page loads but login/data fails** → it's almost always STEP 6f. Re-run it
and confirm `CORS_ALLOW_ORIGINS` and `DASHBOARD_URL` are EXACTLY `$DASHBOARD_URL`.

**To see why a service won't start**:
```bash
az containerapp logs show -n <app-name> -g "$RG" --tail 100
```

---

## When you're done (teardown) — SHARED group, delete only YOUR resources

```bash
for a in collector mock ai backend dashboard; do az containerapp delete -n ca-oil-$a-dev-weu -g "$RG" --yes; done
az containerapp job delete -n ca-oil-migrate-dev-weu  -g "$RG" --yes
az containerapp job delete -n ca-oil-injector-dev-weu -g "$RG" --yes
az containerapp env delete -n "$ENV_NAME" -g "$RG" --yes
az postgres flexible-server delete -n "$PG_NAME" -g "$RG" --yes
az redis delete -n "$REDIS_NAME" -g "$RG" --yes
az acr delete -n "$ACR_NAME" -g "$RG" --yes
az identity delete -n "$MI_NAME" -g "$RG"
az keyvault delete -n "$KV_NAME" -g "$RG"
```
(🤝 also remove the RabbitMQ resource your colleague made. Do NOT `az group delete` —
the group is shared.)

---

## The spots most likely to need your colleague (🤝)
1. **RabbitMQ** setup + its connection string (STEP 1e / 2).
2. **The GitHub Packages token** for the dashboard build (STEP 4b).
3. **Granting the identity access to Key Vault** (STEP 3b) if you can't change the vault.
4. **Postgres networking** choice (STEP 1c `--public-access`).
5. Enabling **pg_cron** (STEP 7).

If any `az` flag is rejected (CLI versions change), run the command with `--help` or
ask your colleague — don't guess.
```
