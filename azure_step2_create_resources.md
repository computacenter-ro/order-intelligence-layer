# Azure Step 2 — create the resources (az CLI, CAF naming)

Run in **Azure Cloud Shell (Bash)**, one at a time. Names follow the Cloud Adoption
Framework: `<type>-<workload>-<env>-<region>-<instance>` (type abbreviations from
CAF: rg, kv, cr, psql, redis, cae, st).

Reusing the EXISTING resource group `rg-internship-2026` — there is **no**
`az group create` step.

🤝 = confirm with your Azure colleague (region, SKU/cost, networking).

---

## 0. Set names once (paste, then edit if a global name is taken)

```bash
# --- context ---
export LOC="westeurope"
export LOCABBR="weu"
export RG="rg-internship-2026"      # EXISTING group — do NOT create it
export WL="oil"                     # workload short name (order intelligence layer)
export ENVN="dev"                   # environment tag
export UNIQ="01"                    # bump to 02, 03... if a GLOBAL name is taken

# --- CAF-style resource names ---
export KV_NAME="kv-${WL}-${ENVN}-${LOCABBR}-${UNIQ}"    # hyphens allowed
export ACR_NAME="cr${WL}${ENVN}${LOCABBR}${UNIQ}"       # ACR: NO hyphens allowed
export PG_NAME="psql-${WL}-${ENVN}-${LOCABBR}-${UNIQ}"
export REDIS_NAME="redis-${WL}-${ENVN}-${LOCABBR}-${UNIQ}"
export ENV_NAME="cae-${WL}-${ENVN}-${LOCABBR}"          # env: no global uniqueness needed

# --- postgres admin ---
export PG_ADMIN="oiladmin"
export PG_PASSWORD="ChangeMe-Str0ng!"                   # 🔑 pick a strong one

# sanity: confirm the right subscription BEFORE creating anything
az account show --query name -o tsv
# az account set --subscription "<name>"   # only if it's the wrong one
```

Note: `kv`, `cr`, `psql`, `redis` names must be **globally unique**. If a create
fails with "name taken", bump `UNIQ` and re-export just that name.

---

## 1. Key Vault (secrets)

```bash
az keyvault create --resource-group "$RG" --name "$KV_NAME" --location "$LOC"
echo "KEY VAULT URI: https://$KV_NAME.vault.azure.net/"
```

## 2. Container Registry (images)  — name has NO hyphens

```bash
az acr create --resource-group "$RG" --name "$ACR_NAME" --sku Basic --admin-enabled true
az acr show --name "$ACR_NAME" --query loginServer -o tsv     # capture the login server
```

## 3. 🤝 PostgreSQL Flexible Server (durable data)

```bash
az postgres flexible-server create \
  --resource-group "$RG" \
  --name "$PG_NAME" \
  --location "$LOC" \
  --admin-user "$PG_ADMIN" \
  --admin-password "$PG_PASSWORD" \
  --tier Burstable --sku-name Standard_B1ms \
  --storage-size 32 \
  --version 16 \
  --public-access 0.0.0.0        # 🤝 allows Azure services; colleague may prefer VNet

az postgres flexible-server db create \
  --resource-group "$RG" --server-name "$PG_NAME" --database-name oil

az postgres flexible-server show \
  --resource-group "$RG" --name "$PG_NAME" \
  --query fullyQualifiedDomainName -o tsv        # capture PG host
```
DATABASE_URL (Key Vault later):
```
postgresql+asyncpg://oiladmin:<PG_PASSWORD>@<PG_HOST>:5432/oil?sslmode=require
```

## 4. 🤝 Azure Cache for Redis (slow: ~15-20 min — start it, then continue)

```bash
az redis create \
  --resource-group "$RG" --name "$REDIS_NAME" --location "$LOC" \
  --sku Basic --vm-size c0         # 🤝 smallest tier; colleague confirms sizing

echo "REDIS HOST: $REDIS_NAME.redis.cache.windows.net:6380"
az redis list-keys --resource-group "$RG" --name "$REDIS_NAME" --query primaryKey -o tsv
```
REDIS_URL (Key Vault later):
```
rediss://:<REDIS_PRIMARY_KEY>@<REDIS_NAME>.redis.cache.windows.net:6380/0
```

## 5. 🤝 RabbitMQ — NO managed Azure service

No `az` command exists. Decide with your colleague: run as a Container App later
(`rabbitmq:3.13-management`, min replicas = 1), a Marketplace offering (CloudAMQP),
or Azure Service Bus (code change). End result is a value:
```
RABBITMQ_URL = amqps://<user>:<pass>@<host>:5671/     # or amqp://...:5672 if internal
```

## 6. Container Apps Environment (shared network)

```bash
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights

az containerapp env create --resource-group "$RG" --name "$ENV_NAME" --location "$LOC"
```

---

## Capture these into azure_deployment_sheet.md

| Value | From |
|---|---|
| Key Vault URI | step 1 |
| ACR login server | step 2 |
| `DATABASE_URL` (with password + `?sslmode=require`) | step 3 |
| `REDIS_URL` (rediss://) | step 4 |
| `RABBITMQ_URL` | step 5 (colleague) |
| Env name (`cae-oil-dev-weu`) | step 6 |

## Names cheat-sheet (CAF)

| Resource | Name | Hyphens? |
|---|---|---|
| Resource group (existing) | `rg-internship-2026` | yes |
| Key Vault | `kv-oil-dev-weu-01` | yes |
| Container Registry | `croildevweu01` | **no** |
| PostgreSQL | `psql-oil-dev-weu-01` | yes |
| Redis | `redis-oil-dev-weu-01` | yes |
| Container Apps env | `cae-oil-dev-weu` | yes |
| (later) Container apps | `ca-oil-backend-dev-weu`, `ca-oil-ai-dev-weu`, … | yes |

## Reminders

- **Costs money while it exists.** These live in a shared internship group, so do
  NOT `az group delete` the whole group at teardown — instead delete each resource
  you created individually (or ask your colleague), e.g.
  `az resource delete --ids <id>`.
- **ACR & storage names allow no hyphens** — that's why `cr...` is run together.
- **If a flag is rejected**, run the command with `--help` or ask your colleague —
  don't guess.
