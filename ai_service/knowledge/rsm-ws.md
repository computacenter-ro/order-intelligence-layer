```yaml
service: "rsm-ws"
aliases: ["RSM Web Service", "RSM Web Services", "RsmWs"]
repo: "rsm-ws"
log_files: ["d:/apps/rsm-ws/rsm-ws.log"]
log_format: "plain text"
host_retention: "10MB per file, daily + index rollover to .gz, maxHistory 7, totalSizeCap 0 (uncapped)"
environments: ["local", "dev", "sit", "test", "cte", "ua2", "preprod", "prod"]
correlation_id: "none — no MDC.put anywhere in tracked source, and the file pattern's correlation slot resolves to empty"
primary_join_key: "customerAccountCode / productCode — but only on DEBUG lines, which are off in prod"
statuses_written: []
```

# rsm-ws

## 1. What this service does

rsm-ws is a read-only lookup API over Computacenter's rebate data. A caller asks it three
kinds of question about one country's commerce database: *what published rebate schemes apply
to this customer and these products*, *is this customer enrolled for this software licence*,
and *what PVC rates apply to these products for this customer*. It answers from the rebate
database and returns JSON. Callers include the order engine — two endpoints are named "oe"
explicitly (`swagger-spec/rsm-ws-openapi.yaml:270`, `:302`).

Every request carries a mandatory `countryIdentifier` query parameter, one of GB, DE, FR, BE,
NL, US (`swagger-spec/rsm-ws-openapi.yaml:407`). That value selects which country's Oracle
database the whole request reads from
(`ControllerExecutionContextImpl.java:26`, `RoutingDataSource.java:19-21`). The same customer
and product asked with a different country identifier is a different question against a
different database.

### 1.1 What rsm-ws is *not* responsible for

Its responsibility both begins and ends with the HTTP response. There is no queue, no
scheduler, no outbound call to another service, and **no write of any kind** — no `save`,
`persist`, `INSERT`, `UPDATE` or `DELETE` appears anywhere in `src/main/java`, and all ten
`@Transactional` annotations are `readOnly = true` (`RebateServiceImpl.java:72` and nine
others). Specifically not this service's job:

- **Creating, editing, approving or publishing rebate schemes.** rsm-ws only filters on
  `RebateSchemeStatus.PUBLISH` when reading (`RebateServiceImpl.java:74`). Whatever sets that
  status is elsewhere.
- **Calculating PVC rates.** The rate values come out of an Oracle PL/SQL package
  (`sql/2025-S12/RQ171766/product-pvc-pkg-body.sql:22-140`) reading `pp_pp01curt.pp_prod_pvc` and
  calling `pvc.pvc_calc_pkg.api_pvc_get_rate` (`:109`). rsm-ws only calls the function and maps
  rows. **Two files in this repo are named `product-pvc-pkg-body.sql`** — always cite the
  `2025-S12/RQ171766` one; the `2024-S40/RQ164577` one is superseded (§2, `vendorPvc`).
- **Customer enrolment and licence agreements.** Read from views, never written.
- **Authentication accounts.** HTTP Basic credentials come from `${rsm.ws.api.username}` /
  `${rsm.ws.api.password}` (`application.properties:25-26`), resolved from the Spring Cloud
  Config server (`bootstrap.properties:5-9`).
- **Anything downstream of the response.** If the order engine prices an order wrongly, rsm-ws
  can only be shown to have returned particular JSON — it has no record of what was done with it.

## 2. Key concepts

| Term | What it actually is, in code |
|---|---|
| `countryIdentifier` | Request parameter that selects the target database for the request. Set into a `ThreadLocal` at `DatasourceContextServiceImpl.java:29`, read back by `RoutingDataSource.java:20`, cleared in a `finally` at `ControllerExecutionContextImpl.java:30`. |
| `rebateSchemeCode` | Computacenter-internal scheme id; primary key column `RBT_SCHM_CD` (`RebateHeader.java:34`), e.g. `MSO_SVR_5_MS__28082011124329462` (`rsm-ws-openapi.yaml:419`). |
| `manufacturerRebateSchemeCode` | The *manufacturer's* scheme code — a different column, `SUP_RBT_SCHM_CD` (`RebateHeader.java:40`). Not interchangeable with `rebateSchemeCode`. |
| `manufacturerRebateSchemeType` | Column `RBT_SCHM_CLS_CD`, stored as one letter: R/U/D/S/M/I → RETROSPECTIVE, UPFRONT, DISTRIBUTOR, SOFTWARE, MIXED, INTERNAL (`ManufacturerRebateSchemeType.java:9`). |
| `subSchemeTypeCode` | Column `RBT_SUB_SCHM_TYP_CD`, stored as `I` or `B` → ITEM or BUNDLE (`SubSchemeTypeCode.java:9`). |
| `RebateSchemeStatus` | Enum with four values DRAFT, PROVISIONAL, PUBLISHED_DRAFT, PUBLISH (`RebateSchemeStatus.java:8`). Only `PUBLISH` is ever queried for (`RebateServiceImpl.java:74`). |
| `productCode` | SAP material number, 3–10 chars (`rsm-ws-openapi.yaml:411-415`); column `PROD_CD` (`RebatedProductCostPk.java:26`). |
| `customerAccountCode` | SAP Sold-To partner number (`rsm-ws-openapi.yaml:778-781`); column `CUS_ACC_CD` (`RebatedProductCost.java:71`). |
| `eligibleCustomerCount` | Number of customer accounts in the scheme's customer group — a **count, not an id**, obtained by `countByCustomerGroupCode` (`CustomerGroupExpansionDao.java:14`) and injected at `ProductRebateResponseMapperDecorator.java:32-33`. |
| `thresholdQuantity` | Derived, not stored: `MAX_QTY + ADJ_QTY` (`ProductRebateResponseMapperDecorator.java:41-42`). |
| `claimedQuantity` | Derived: authorised claims + unauthorised claims (`:44-45`). |
| `availableQuantity` | Derived: threshold − (claimed + invoiced + ordered) (`:47-49`). Can go negative — nothing clamps it. |
| `standardPvc` | `pvc_rate` from `pp_pp01curt.pp_prod_pvc` where both `cus_acc_cd` and `sup_cd` are NULL (`RQ171766/product-pvc-pkg-body.sql:56-64`). |
| `customerSpecificPvc` | Same table, matched on `cus_acc_cd = p_sold_to` with `sup_cd` NULL (`:74-82`). |
| `vendorPvc` | A JSON array built per active supplier of the product, each entry `vendorNumber` / `vendorPvcRate` / `supplierRanking` (`:114-118`). Returned from Oracle as a CLOB and parsed in Java (`ProductPvcRowMapper.java:42`). **`vendorPvcRate` reads the OUT parameter `l_pvc_rate`, not the function's return value** (`:116`) — that one-word change *is* changeset RQ171766, whose header records the defect it fixes: `IN110224 \| RSM WS - Vendor PVC rate returns 1 rather than the expected value` (`:18-19`). The superseded body put `l_pvc_rate_result` there instead (`RQ164577/product-pvc-pkg-body.sql:114`). **So a country database that has not had changeset `RQ171766-pvc-product-pvc-pkg` applied returns `vendorPvcRate` = 1 for every vendor** — see §12. |
| "unenrolled software licence" | A requested product whose item-class code is `SOLI` **and** for which no row exists in `V_RBT_CST` for that customer with `rbt_schm_cls_cd = 'S'` (`SoftwareLicenceQueryDaoImpl.java:30-34`). |
| V1 vs V2 | Same data. V2 exists solely to fix misspelled JSON property names — `bunndleItems` → `bundleItems` (`ProductRebateResponseMapper.java:90`), `customerAccontCode` → `customerAccountCode` (`RebateServiceImpl.java:241`). V1 methods are `@Deprecated(since = "V2")` (`RebateService.java:73`, `:96`). |
| `V_RBT_CST` | The `MARTINI_CUSTOM` view every product-rebate query hits (`RebatedProductCost.java:23`). |

### 2.1 Concepts this document does not cover

Nothing in this repo defines these; decline rather than infer:

- **What "PVC" stands for.** The acronym is never expanded anywhere in the codebase.
- **What "BSR" means** (`l_post_bsr_threshold`, `product-pvc-pkg-body.sql:33`).
- **`CommerceDatasourceAlias` values `GRP`, `XA`, `XP`, `XR`** (`CommerceDatasourceAlias.java:8`) — declared but no datasource is configured for them and the API enum cannot express them.
- **What `com.computacenter.standardized.controller.ControllerHelper.catchAndLog` does** (`ControllerExecutionContextImpl.java:28`) — it is the wrapper around *every* endpoint, so it decides what exception logging and what HTTP status a caller sees, and its source is not in this repo. Same for `StandardizedControllerExceptionHandler` (`ControllerExceptionHandler.java:13`) and `getAuthenticationExclusionUrls()` (`WebSecurityConfig.java:41`).
- **The Tomcat access log's file name, format and retention.** Only its directory is set here (`application.properties:23`).
- **Rate limits, quotas, request size caps.** None configured in this repo.

## 3. How work flows through this service

Every endpoint runs the same three steps, in this order:

1. Log the request at DEBUG (nine of eleven endpoints — see §3.1).
2. `ControllerExecutionContextImpl.executeInDatasourceContext`: set the country into the
   `ThreadLocal`, run the service call inside `controllerHelper.catchAndLog`, clear the
   `ThreadLocal` in `finally` (`ControllerExecutionContextImpl.java:26-31`).
3. Map the entity/projection to a response DTO and return.

The three JDBC routes taken inside step 2 differ by endpoint:

| Route | Bean | Schema reached | Used by |
|---|---|---|---|
| JPA, `dao.rsm` | `rsmDataSource` (`RsmDataSourceConfig.java:66`) | `PP_BASECURT`, `PP_PP01CURT` | rebate header, distributors, customer-group count |
| JPA + QueryDSL, `dao.custom` | `customDataSource` (`CustomDataSourceConfig.java:65`) | `MARTINI_CUSTOM.V_RBT_CST` | all product-rebate and software-licence lookups |
| Plain JDBC | `pvcJdbcOperations` (`PvcDataSourceConfig.java:179`) | `pvc.product_pvc_pkg` | the two PVC endpoints |

The unenrolled-software-licence query is **batched in chunks of 100 product codes**
(`SoftwareLicenceQueryDaoImpl.java:28`, `:63-69`), so one call with 250 products issues three
SQL statements. Nothing logs the batching.

Two endpoints do extra per-row work rather than a single query. `getRebateDetailsByCustomerAndProduct`
loops over each matched rebate row and, **per row**, fires an extra query if the scheme type is
`SOFTWARE` (`RebateServiceImpl.java:184-189`) and another if the sub-scheme type is `BUNDLE`
(`:198-201`). A request for many products against bundle schemes is therefore N+1 queries, not one.

### 3.1 Why a step gets skipped

- **"I called the API and there is nothing in the log."** Two of the eleven endpoints log
  nothing at all, at any level: `getCartProductRebatesByCustomer`
  (`RebatesApiController.java:112-124`) and `getDistributors` (`:126-135`). And in prod all
  eleven are silent — see §7.1.
- **Software band / scheme / valuation type missing from an "oe" response.** Only filled when
  the row's scheme type is exactly `SOFTWARE` (`RebateServiceImpl.java:184`), and only if the
  extra query found a row (`:190`). A `MIXED` scheme containing software gets nothing.
- **`bundleItems` empty.** Only populated when `subSchemeTypeCode` is `BUNDLE`
  (`RebateServiceImpl.java:198`).
- **`vendorPvc` missing while `standardPvc` is present.** The PL/SQL only computes customer-specific
  and vendor PVC inside `IF p_sold_to IS NOT NULL` (`RQ171766/product-pvc-pkg-body.sql:71-129`), or
  the JSON failed to parse (§4.4).
- **A product you asked about is absent from a rebate response but present in a PVC response.**
  The PVC function pipes one row per *requested* material id unconditionally
  (`RQ171766/product-pvc-pkg-body.sql:131`), so unknown products come back with null rates. The rebate
  queries filter with `productCode.in(...)` (`RebateServiceImpl.java:103`), so unmatched products
  simply do not appear.
- **Second identical call produced no log lines and no DB query.** Three results are cached for
  300 seconds (`ehcache.xml:13`): `distributors`, `customerGroupToAccount`,
  `customerEnrolledLicences` (`:25`, `:32`, `:39`).

## 4. The rules that stop something

These are the only in-repo gates. All are **faults or bad requests**, not business decisions
requiring a human — except 4.1, which is a data condition.

### 4.1 Scheme must be published — rebate header endpoint

`findByRebateSchemeCodeAndRebateStatus(rebateSchemeCode, RebateSchemeStatus.PUBLISH, …)`
(`RebateServiceImpl.java:74`). A scheme in DRAFT, PROVISIONAL or PUBLISHED_DRAFT is
indistinguishable from one that does not exist: `ResponseEntity.of(Optional.empty())` →
**HTTP 404 with no body and no log line** (`RebatesApiController.java:54`). This is a
**business** answer, not a fault — the scheme needs publishing, not a ticket.

### 4.2 Scheme-code / version / sub-scheme combination

`getProductRebatesForCustomer` rejects a version or sub-scheme code supplied without a scheme
code, throwing `IllegalArgumentException` with this message — note the missing space, which is
in the source and will appear in any output that echoes it:

> `Manufacturer rebate scheme code cannot be blank whenversion and/or sub scheme code is provided`

(`RebateServiceImpl.java:85-86` — two concatenated literals, `"…blank when"` + `"version…"`.)

`getProductRebatesForCustomerRebate` has a separate, differently-worded check:

> `Manufacturer rebate scheme code cannot be blank`

(`RebateServiceImpl.java:115`.)

### 4.3 Blank customer / empty product list — "oe" and PVC endpoints only

> `Customer Account Code cannot be blank`  (`RebateServiceImpl.java:168`, `:212`)
> `Product codes cannot be blank`  (`RebateServiceImpl.java:172`, `:216`)

Note "Product codes cannot be blank" is raised by `CollectionUtils.isEmpty` — it means *empty
list*, not blank strings. The software-licence path uses different wording again, via
`Assert.notNull`: `customerAccountCode is required` and `productCode is required`
(`SoftwareLicenceServiceImpl.java:31`, `:42`, `:43`), and returns an empty list rather than
throwing for an empty product list (`:32-34`).

### 4.4 Unparseable vendor PVC JSON — silent partial success

`ProductPvcRowMapper` catches `JacksonException`, logs WARN, and returns the DTO with
`vendorPvc` left null (`ProductPvcRowMapper.java:44-47`). The caller gets **HTTP 200 with
silently missing vendor data**. See §9 for the exact text.

### 4.5 Bean-validation constraints

`productCode` path parameter must be 3–10 chars (`SoftwareLicenceApiController.java:53`);
`page` must be 1–999 (`RebatesApiController.java:90`). `countryIdentifier` is a required enum,
so an unrecognised country is rejected before any of this service's code runs.

The **HTTP status and response body** for every case in 4.2–4.5 is decided by
`StandardizedControllerExceptionHandler` and `catchAndLog`, which are not in this repo — see §2.1.

## 5. What users can do

There is no UI. Callers are machines using **HTTP Basic** auth (`WebSecurityConfig.java:39`),
one shared credential pair (`application.properties:25-26`), sessions stateless
(`WebSecurityConfig.java:43`). Everything under `/api/**` and `/shutdown` requires
authentication (`:32`, `:42`).

Eleven endpoints (`swagger-spec/rsm-ws-openapi.yaml`): rebate scheme header; product rebates by
customer; all rebate products for a customer scheme (paged); cart product rebates; distributors;
unenrolled software licences; enrolled software licence for one product; and "oe" rebate details
and PVC details in both V1 and V2.

Causes of "I can't call it", in priority order:

1. **401** — bad or missing Basic credentials. The response carries header `WWW-Authenticate: Basic`
   and message `Not authorized` (`WebSecurityConfig.java:56-57`).
2. **Wrong scheme/port.** HTTPS on 8097 is the default; plain HTTP is still served on **7097**
   as a documented temporary migration measure (`application.properties:2`, `:13`,
   `TomcatConfig.java:29-36`).
3. **Swagger UI unavailable in prod** — `SwaggerConfig` is annotated `@Profile("!(prod | junit)")`
   (`SwaggerConfig.java:22`). This is **silent**: the docs endpoints simply are not there. Deliberate.
4. **404 on the header endpoint** — see §4.1. Also silent, and it means "not found *or* not published".
5. **A country other than GB/DE/FR/BE/NL/US** — rejected by the enum, not by this service's code.

No per-user rights, no locking, no concurrency control, no status-based edit restrictions exist
here — nothing to check, because nothing is written.

## 6. Statuses, and who writes them

**This service writes exactly 0 statuses.** Verified by grepping `src/main/java` for `save`,
`saveAll`, `delete`, `persist`, `INSERT`, `UPDATE`, `DELETE`, `MERGE` — no matches.

| Status value | Table / column | Written by | Read by rsm-ws |
|---|---|---|---|
| `PUBLISH` (also DRAFT, PROVISIONAL, PUBLISHED_DRAFT) | `PP_BASECURT.RBT_SCHM.REBATE_STATUS` (`RebateHeader.java:53-55`) | not this service | yes — `PUBLISH` only (`RebateServiceImpl.java:74`) |
| `REC_STS_CD` | `PP_BASECURT.RBT_SCHM` (`RebateHeader.java:69-70`) | not this service | mapped onto the entity but **never filtered on and never returned** — the API's header DTO has no such field |

The one thing rsm-ws does write is DDL: at startup it runs Liquibase against each of the six PVC
datasources (`PvcDataSourceConfig.java:154-177`), creating/replacing the `product_pvc_pkg` package
(`changelog-RQ171766.xml:16-19`).

---

## 7. Log anatomy — read this before quoting any line

Config: `src/main/resources/logback-spring.xml`, with `scan="true"` so edits are picked up
without a restart (`:2`). It defines only a `LOG_FILE` fallback and then includes three Spring
Boot resources — `defaults.xml`, `console-appender.xml`, `file-appender.xml` (`:5-7`).

**The file name on disk is not the one written in `logback-spring.xml`.** Line 4 defaults
`LOG_FILE` to `…/rsmws.log`, but `logging.file.name = d:/apps/rsm-ws/rsm-ws.log`
(`application.properties:22`) makes Spring Boot set `LOG_FILE` before Logback initialises, so
the real file is **`rsm-ws.log`** (with a hyphen). In `local` and `sit` it is
`c:/temp/logs/rsm-ws/rsm-ws.log` (`application-local.properties:4`, `application-sit.properties:3`).

The effective file pattern is Spring Boot's `FILE_LOG_PATTERN`, unmodified — no `logging.pattern.*`
property is set anywhere in this repo. Its parts:

```
<ISO-8601 timestamp with offset> <level, %5p> <pid> --- <app name>[<thread>] <logger, %-40.40> : <message>
```

A constructed example (derived from the pattern, not copied off a host):

```
2026-07-28T09:14:02.417+01:00 DEBUG 8412 --- [http-nio-8097-exec-3] c.c.r.controller.RebatesApiController     : Received request to get product rebates ManufacturerRebateSubschemeRequestDto[manufacturerRebateSchemeCode=MSO_SVR_5, manufacturerRebateSchemeVersion=0001, subSchemeCode=null, customerAccountCode=81043279, page=null]
```

Parsing hazards:

- **A timezone offset *is* recorded** (the `XXX` in the timestamp), so lines are unambiguous
  across DST — but it is host-local, not UTC.
- **The level is right-padded to five characters**, so `INFO` and `WARN` are preceded by a
  space: ` INFO`, ` WARN`. `DEBUG` and `ERROR` are not. Grepping `"WARN "` misses them.
- **Fully-qualified class names never appear.** The logger is rendered `%-40.40logger{39}` —
  abbreviated to ≤39 chars, then padded/truncated to exactly 40. Searching for
  `com.computacenter.rsmws.controller.RebatesApiController` finds nothing. The abbreviated
  forms of the eight classes that log are:

  | Class | As it appears |
  |---|---|
  | `RsmCacheEventListener` | `c.c.rsmws.cache.RsmCacheEventListener` |
  | `DatasourceContextServiceImpl` | `c.c.r.c.d.DatasourceContextServiceImpl` |
  | `RebatesApiController` | `c.c.r.controller.RebatesApiController` |
  | `RebatesApiControllerV2` | `c.c.r.controller.RebatesApiControllerV2` |
  | `SoftwareLicenceApiController` | `c.c.r.c.SoftwareLicenceApiController` |
  | `SoftwareLicenceQueryDaoImpl` | `c.c.r.d.c.SoftwareLicenceQueryDaoImpl` |
  | `ProductPvcRowMapper` | `c.c.r.dao.mapper.ProductPvcRowMapper` |
  | `RebateProductPvcQueryDaoImpl` | `c.c.r.d.p.RebateProductPvcQueryDaoImpl` |

  Note `c.c.r.c.SoftwareLicenceApiController` and `c.c.r.c.d.DatasourceContextServiceImpl` both
  abbreviate `controller`/`config` to `c`. **Match on message text, not on logger name.**
- **Stack traces go into the same file, inline**, appended to the line that logged them by
  `%wEx` at the end of the pattern, then continuation lines with no timestamp prefix. Joining
  rule: a line that does not begin with a 4-digit year belongs to the line above it.
- **`ERROR` in the level column is not always a fault** — see §9.1.

### 7.1 Level availability per environment

`logback-spring.xml` sets `<root level="INFO">` with only the `FILE` appender (`:8-10`), then:

| Profile | `com.computacenter.rsmws` | `org.springframework`, `org.hibernate.SQL` | Console |
|---|---|---|---|
| `prod` | **INFO** (inherited from root) | INFO | no |
| `dev`, `test`, `preprod`, `cte`, `ua2` | DEBUG (`:33-37`) | INFO | no |
| `sit` | DEBUG | DEBUG, plus TRACE on `…jpa.repository.query` and `…BasicBinder` | yes |
| `local` | DEBUG (`:28-31`) | as `sit` | yes |

`sit` appears in **both** `springProfile` blocks (`:11`, `:33`) — that is an uncommitted
working-tree change, see §13.

**The prod-visibility story, in numbers.** Counting `log.*(` across `git ls-files '*.java'`
(15 statements total, all in `src/main/java`; test sources contain none):

| Level | Statements | Visible in prod? |
|---|---|---|
| TRACE | 0 | — |
| DEBUG | 12 | **no** |
| INFO | 0 | — |
| WARN | 1 | yes |
| ERROR | 2 | yes |

So **80% of this service's own log statements are DEBUG, and there are no INFO statements at
all.** In prod, the only three lines rsm-ws can ever emit from its own code are the one WARN and
two ERRORs in §9. A prod log file with no rsm-ws lines in it is the normal, healthy state and
proves nothing about whether a request arrived.

This does *not* mean the prod file is empty — Spring, Hibernate, Hikari and Tomcat log at INFO
via the root logger. Note also that the `local`/`sit` loggers are `additivity="false"`, so those
categories are routed *only* to their listed appenders and not additionally through root.

### 7.2 Retries

**There are none.** No `@Retryable`, `RetryTemplate`, resilience4j, circuit breaker, `RestTemplate`,
`WebClient`, Feign client, `@Async`, AMQP or JMS anywhere in `src/main` or `pom.xml`. There is no
outbound HTTP call to retry and no queue to redeliver from.

Consequences for reading logs: **every log line is one occurrence of one event.** Never collapse
duplicates as retries — two identical lines are two separate inbound requests. Conversely, a
transient Oracle failure produces exactly one exception on exactly one request; there is no
second attempt.

The only configured time limits are on the JDBC pools, and they *are* applied — bound onto
`HikariConfig` at `CommonDatasourceConfig.java:20` and used to build every datasource
(`HikariDataSourceFactory.java:25-30`): `connection-timeout = 30000`, `validation-timeout = 10000`,
`idle-timeout = 60000`, `maximum-pool-size = 10` (`datasourceconfig.properties:11-18`; 3 in
`local`/`sit`, `datasourceconfig-local.properties:19`; 5 in `dev`, `datasourceconfig-dev.properties:1`).
There are **eighteen** pools (3 schemas × 6 countries), each with its own limit — exhausting the
GB custom pool does not affect DE.

Dead config to ignore: `common.datasource.hikari.pool-name = rsm`
(`datasourceconfig.properties:19`) is not the pool name that appears in Hikari's log lines. Pool
names are set per datasource from `<cc>.<schema>.datasource.pool-name` — `gb-rsm`, `de-custom`,
`us-pvc` and so on (`datasourceconfig.properties:25`, `HikariDataSourceFactory.java:29`). **The
pool name in a Hikari log line is the only reliable indicator of which country a low-level DB
error belongs to.**

### 7.3 Retention and file names

Rolling is Spring Boot's `file-appender.xml` default `SizeAndTimeBasedRollingPolicy`, entirely
unoverridden — no `logging.logback.rollingpolicy.*` property exists in this repo:

- Active file: `d:/apps/rsm-ws/rsm-ws.log` (`application.properties:22`)
- Rolled: `d:/apps/rsm-ws/rsm-ws.log.<yyyy-MM-dd>.<index>.gz`
- 10MB per file, `maxHistory` 7, `totalSizeCap` 0 (uncapped), `cleanHistoryOnStart` false

**Practical limit: roughly seven days.** On a busy day the index can climb, and evidence older
than a week is likely gone — timestamp windows must be requested from support quickly.

The access log directory is `d:/apps/rsm-ws` too (`application.properties:23`), i.e. two
different log streams in one directory.

## 8. Identifiers in the logs

There is **no correlation id and no request id**. `MDC` appears nowhere in `src/` — zero
`MDC.put` calls — and the pattern's correlation slot resolves to empty because nothing sets
`logging.pattern.correlation`. **Two concurrent requests cannot be told apart except by thread
name** (`[http-nio-8097-exec-N]`), which is reused across requests, so it is a within-a-few-
milliseconds hint at best, not a join key.

What does appear, and only on DEBUG lines (so: not in prod):

| Identifier | The message text with a value substituted (illustrative, not copied off a host) | Scope |
|---|---|---|
| `rebateSchemeCode` | `…header details for MSO_SVR_5_MS__28082011124329462 in country GB` | cross-service — the same code the order engine and the rebate system use |
| `customerAccountCode` | `…for customer 81043279 and product 2222221` | cross-service (SAP Sold-To) |
| `productCode` list | `…for the products: [2844804, 2844805]` — Java `List.toString()`, square brackets, comma-space | cross-service |
| Request DTO | `ManufacturerRebateSubschemeRequestDto[manufacturerRebateSchemeCode=…, manufacturerRebateSchemeVersion=…, subSchemeCode=…, customerAccountCode=…, page=…]` — Java record `toString()` (`ManufacturerRebateSubschemeRequestDto.java:10-11`), `null` printed literally for absent fields | cross-service |
| datasource alias | `Setting datasource context to: GB` | local |
| cache key | `Cache event 'CREATED' fired for key '…'` | local |

**Nothing identifies the caller.** No username, client id or source IP is logged by this
service at any level. "Who called this?" can only be answered from the Tomcat access log
(§10), never from `rsm-ws.log`.

## 9. Journey stages and their log evidence

All 15 statements, with level and emitting class. `{}` placeholders preserved verbatim.

**Stage 1 — request received** (DEBUG; nine endpoints, two log nothing):

| Message text | Class |
|---|---|
| `Received request to get basic rebate scheme header details for {} in country {}` | `RebatesApiController` (`:50`) |
| `Received request to get product rebates {}` | `RebatesApiController` (`:73` and `:100` — **two endpoints share identical text**, so a match alone does not tell you which was called; `:100` is the paged one and its DTO renders a non-null `page=`) |
| `Received request to get rebates for the products: {}` | `RebatesApiController` (`:143`) **and** `RebatesApiControllerV2` (`:36`) — identical text; only the `V2` in the abbreviated logger name distinguishes them |
| `Received request to get pvc details for the products: {}` | `RebatesApiController` (`:157`) **and** `RebatesApiControllerV2` (`:50`) — same caveat |
| `Received request to validate products as software licences country {}, for customer {} : {}` | `SoftwareLicenceApiController` (`:38`) — note the space before the colon |
| `Received request to get software licence for customer {} and product {}` | `SoftwareLicenceApiController` (`:56`) |

Do not assume this family is uniform: three of the six wordings are reused by more than one
endpoint, and two endpoints (`getCartProductRebatesByCustomer`, `getDistributors`) emit nothing.

**Stage 2 — datasource selected** (DEBUG, `DatasourceContextServiceImpl`):

| Message text | Line |
|---|---|
| `Setting datasource context to: {}` | `:28` |
| `Clearing existing datasource context: {}` | `:46` |

These bracket every request. A "Setting" with no matching "Clearing" means the thread died
inside the request.

**Stage 3 — cache** (DEBUG, `RsmCacheEventListener`, `:17-18`):

`Cache event '{}' fired for key '{}' and value: {}`

Fired asynchronously on CREATED, EXPIRED and EVICTED only (`ehcache.xml:17-21`) — **cache
*hits* produce no line at all.** Being asynchronous and unordered (`:17-18`), it can appear out
of sequence relative to the request that caused it. The third placeholder prints the whole cached
value, so a `distributors` CREATED event dumps the entire distributor list into the log.

**Stage 4 — data problems** (the only three prod-visible lines):

| Level | Message text | Class |
|---|---|---|
| WARN | `Cannot parse JSON response in ProductPvcDto` | `ProductPvcRowMapper` (`:45`) — no placeholders; the exception is passed as the second argument, so a stack trace follows |
| ERROR | `Software license details not found for the rebate scheme code {}` | `SoftwareLicenceQueryDaoImpl` (`:82`) |
| ERROR | `PVC details not found for the products {}` | `RebateProductPvcQueryDaoImpl` (`:56`) |

Note the ERROR at `SoftwareLicenceQueryDaoImpl:82` logs the *rebate scheme code* only — the
product code is a method parameter (`:74`) but is **not** in the message, so you cannot tell
which product triggered it.

### 9.1 Lines that look like errors but are not

- **`Software license details not found for the rebate scheme code {}`** — ERROR level, but it is
  a routine data gap: `EmptyResultDataAccessException` caught, `null` returned
  (`SoftwareLicenceQueryDaoImpl.java:81-84`), and the request still returns **HTTP 200**, just
  without `softwareBand` / `softwareLicenseScheme` / `softwareLicenseValuationType`
  (`RebateServiceImpl.java:190-195`). Do not raise a ticket against rsm-ws for this; the missing
  row is in `pp_pp01curt.pp_rbt_schm_prod`. It says "not found", **not** "failed".
- **`PVC details not found for the products {}`** — ERROR level, and effectively unreachable.
  It is in a `catch (EmptyResultDataAccessException)` around
  `pvcJdbcOperations.query(sql, params, rowMapper)` (`RebateProductPvcQueryDaoImpl.java:53-57`),
  but `query(...)` with a `RowMapper` returns an empty `List` for no rows rather than throwing —
  that exception comes from `queryForObject`. **If you ever do see this line, treat it as
  unexplained and escalate**, because nothing in this repo shows how it fires. Correspondingly,
  a PVC request that finds nothing logs **nothing** and returns 200 with an empty `products` array.
- **`Cannot parse JSON response in ProductPvcDto`** — WARN, but the more serious of the three in
  effect: the caller still gets 200 and simply has no vendor PVC data (§4.4).
- **Wrong vendor PVC rates produce no log line whatsoever.** If a country is missing changeset
  RQ171766, every `vendorPvcRate` comes back as 1 and the log is completely clean (§2, `vendorPvc`).
  A "PVC rates are wrong" report with an empty log is expected, not contradictory.
- **Startup**: `preConditions onFail="HALT"` requires the connection to be Oracle and the user to
  be `PVC` (`pvc-changelog_UK.xml:9-14`). If unmet, Liquibase halts and the
  `pvcDataSource` bean creation throws `BeanCreationException("targetableLiquibaseInstances", …)`
  (`PvcDataSourceConfig.java:174`) — that aborts startup. It is not benign.

### 9.2 Queue / topology reference

**There is no messaging.** No queue, exchange, routing key, dead-letter exchange, error queue or
consumer exists in this repo — no AMQP, JMS or Kafka dependency in `pom.xml`, no broker config in
any properties file. Any question about a stuck message, a DLQ or a redelivery belongs to another
service.

## 10. Evidence that is not in the log file

- **Tomcat access log** — directory `d:/apps/rsm-ws` (`application.properties:23`);
  `c:/temp/logs/rsm-ws` in `local`/`sit`. **APPEND-ONLY HISTORY, and the only record that a
  request happened at all in prod.** Its file name, format and retention are configured in
  `cc-standardized-web-app`, not here.
- **Actuator endpoints**, base path `/` (`actuatorconfig.properties:1`), all disabled by default
  and five explicitly enabled: `health`, `info`, `loggers`, `refresh`, `shutdown` (`:3-8`).
  **CURRENT-STATE SNAPSHOT.** Three operational notes: `health` shows no details
  (`show-details = never`, `:9`); `loggers` is writable, so **someone can raise
  `com.computacenter.rsmws` to DEBUG in prod at runtime** — an unexplained flood of
  "Received request…" lines in a prod file means exactly that; and `shutdown` is enabled and
  reachable, protected only by the same single Basic credential (`WebSecurityConfig.java:32`).
- **`PVCDATABASECHANGELOG` / `PVCDATABASECHANGELOGLOCK`** in the `pvc` schema of each of the six
  country databases (`datasourceconfig.properties:121-122`). **APPEND-ONLY HISTORY** — the record
  of which PVC package version each country actually has, and the place to look when one country's
  PVC answers differ from another's. Two changesets should be present: `RQ164577-pvc-product-pvc-pkg`
  (`changelog-RQ164577.xml:16`) and `RQ171766-pvc-product-pvc-pkg` (`changelog-RQ171766.xml:16`).
  **A country missing the second one returns `vendorPvcRate` = 1 for every vendor** (§2). Note
  RQ171766 carries `<validCheckSum>ANY</validCheckSum>` (`:17`), so a locally edited package body
  will not be detected as drifted. A stuck lock row blocks startup.
- **Source tables and views read** — all CURRENT-STATE SNAPSHOTS, none written by rsm-ws:
  `MARTINI_CUSTOM.V_RBT_CST` (`RebatedProductCost.java:23`) · `…V_ROSETTA_PRODUCT_DATA`
  (`SoftwareLicenceQueryDaoImpl.java:31`) · `pp_pp01curt.pp_rbt_schm_prod` (`:42`) ·
  `pp_pp01curt.pp_prod_pvc`, `martini_custom.supplier_pricing`, `martini_main.product`
  (`RQ171766/product-pvc-pkg-body.sql:60`, `:102-103`) · `martini_main.v_product_search`,
  `pp_basecurt.prod` (`BundleRebateQueryDaoImpl.java:32-33`) · `PP_BASECURT.RBT_SCHM`
  (`RebateHeader.java:27`) · `PP_BASECURT.DISTRIBUTORS` (`Distributor.java:16`) ·
  `PP_PP01CURT.PP_CUS_GRP` (`CustomerGroupExpansion.java:16`).
- **No audit columns.** `V_RBT_CST` and `RBT_SCHM` as mapped here expose no
  `created_by` / `modified_by` / `created_date` (`RebatedProductCost.java`, `RebateHeader.java`).
  **"Who changed this rebate?" cannot be answered from anything rsm-ws reads.**
- **Secrets — never quote these.** Jasypt `ENC(...)` values for three Oracle passwords
  (`datasourceconfig.properties:2`, `:5`, `:8`; overridden for `ua2` in
  `application-ua2.properties:4-5`), the keystore password (`application.properties:8`), the
  Cloud Config Server URIs (`bootstrap.properties:9` and each `bootstrap-<env>.properties`), and
  the committed keystore file `src/main/resources/static/ssl/rsmws.computacenter.com.pfx`. They
  decrypt with `encrypt.key` (`bootstrap.properties:2`), supplied at runtime.

## 11. Blind spots and traps

1. **No correlation id on any path, and no request id at all.** Not "missing on the HTTP path" —
   missing everywhere, because there is no other path. Zero `MDC.put` in `src/`. Interleaved
   requests cannot be separated. (Nothing can leak between requests either, since nothing is put.)
2. **In prod the service is effectively silent about its own work** — 12 of 15 statements are
   DEBUG and prod runs at INFO (§7.1). "There's nothing in the log" is never evidence that a
   request did not arrive. Use the access log.
3. **The `ThreadLocal` country context is cleared in a `finally`** (`ControllerExecutionContextImpl.java:29-31`),
   so it does not leak across pooled request threads. But it is **not propagated to any other
   thread** — the cache-event listener runs asynchronously (`ehcache.xml:17`) and
   `DatasourceContextKeyGenerator.generate` dereferences `datasourceContextService.get()`
   unconditionally (`DatasourceContextKeyGenerator.java:24`), so a cacheable method invoked from
   a thread with no country set throws `NullPointerException`. No such path exists via the
   controllers today.
4. **All three routing datasources fall back silently.** `setLenientFallback(true)` plus
   `setDefaultTargetDataSource(dsMap.values().iterator().next())`
   (`RsmDataSourceConfig.java:77-78`, `CustomDataSourceConfig.java:74-75`,
   `PvcDataSourceConfig.java:125-126`) means an unmapped country key returns data from an
   arbitrary country's database with **no error and no log line**. The default target is whichever
   entry `HashMap` iteration happens to yield first — not a declared choice. Currently unreachable
   through the API (the enum permits only the six configured countries), but it is the failure mode
   to suspect if a caller ever reports data for the wrong country.
5. **`spring.liquibase.contexts` is not read by any code in this repo.** It is set per environment
   (`application-prod.properties:4`, and `dev`/`test`/`cte`/`preprod`/`ua2`), but
   `LiquibaseAutoConfiguration` is excluded (`RsmWsApplication.java:13`) and this app's own
   `SpringLiquibaseProperties` binds only `enabled` (`SpringLiquibaseProperties.java:16`). The
   contexts actually applied come from `<cc>.liquibase.props.contexts`, which is **`legacy` in
   every country** (`datasourceconfig.properties:125`, `:132`, `:139`, `:146`, `:153`, `:160`).
   Do not tell anyone a changeset was skipped because of an environment context.
6. **Two message texts are shared by two endpoints each** (§9), and one — `Received request to get
   product rebates {}` — is shared by two endpoints *in the same class*, so even the logger name
   cannot disambiguate. Use the presence of `page=` in the DTO.
7. **Misleading wording to watch:** `eligibleCustomerCount` is a *count of accounts in a group*,
   not an id and not a count of who used the rebate (`ProductRebateResponseMapperDecorator.java:32-33`);
   `thresholdQuantity`, `claimedQuantity` and `availableQuantity` are **computed in Java, not read
   from the database** (`:41-49`), so they can disagree with a direct SQL query against
   `V_RBT_CST`; `availableQuantity` may be negative.
8. **Failures that produce no log line and a 2xx or 4xx response:** a rebate scheme that is not
   published → bare 404 (§4.1); a software licence not found → bare 404
   (`SoftwareLicenceApiController.java:61`, confirmed by
   `SoftwareLicenceApiControllerIntegrationTest.java:149`); PVC found nothing → 200 with an
   empty array; a cart or distributor request → nothing logged even at DEBUG.
9. **PII in log text.** The DEBUG lines print customer account codes and full product lists;
   the cache-event line can print an entire cached result object. Redact customer account codes
   when quoting log excerpts outside the support channel.
10. **`ERROR` does not mean fault** for two of the three prod-visible lines — see §9.1.
11. **Four PL/SQL files, two names, two of them superseded.** Only the files under
    `src/main/resources/static/db/liquibase/sql/` are ever applied — `changelog-RQ164577.xml:18-19`
    applies the RQ164577 specification *and* body, then `changelog-RQ171766.xml:18` replaces the
    body with the RQ171766 version. So:
    - `sql/2024-S40/RQ164577/product-pvc-pkg-body.sql` is **superseded**, and quoting its
      line 114 (`l_pvc_rate_result`) as current behaviour is exactly the mistake to avoid.
    - `src/db/schemas/pvc/ddl/packages/product_pvc_pkg_1.sql` and `product_pvc_pkg.sql` are
      **not referenced by any changelog** (verified: nothing under `static/` mentions them). They
      are byte-identical to the RQ171766 body and the RQ164577 specification respectively *as of
      this commit*, so they carry no divergent text today — but they are unreferenced copies and
      can drift silently. Cite the `static/db/liquibase/sql/` paths, never `src/db/schemas/**`.
12. **The parent library is unread.** `catchAndLog` wraps every single endpoint
    (`ControllerExecutionContextImpl.java:28`) and `StandardizedControllerExceptionHandler`
    handles every exception (`ControllerExceptionHandler.java:13`). Any claim about what an
    exception logs, at what level, with what wording, or what HTTP status the caller sees is
    **outside this document's evidence** — see §2.1.
13. **`logback-spring.xml` has `scan="true"`** (`:2`), so the deployed logging config may differ
    from what a restart would produce; combined with the writable `loggers` actuator endpoint,
    the levels in §7.1 are the *configured* levels, not a guarantee of the running ones.

## 12. Escalation

| Symptom | Route | Business or fault? |
|---|---|---|
| Rebate scheme returns 404 / rebate missing for a customer or product | Rebate scheme owners — the scheme needs publishing, or the customer/product is not on it (§4.1) | **Business.** No ticket against rsm-ws. |
| `Software license details not found for the rebate scheme code …` | Rebate data owners — missing row in `pp_pp01curt.pp_rbt_schm_prod` | **Business/data.** Not an rsm-ws fault despite ERROR level. |
| `Cannot parse JSON response in ProductPvcDto` | rsm-ws / PVC PL/SQL owners — `vendor_pvc` CLOB is not valid JSON (`RQ171766/product-pvc-pkg-body.sql:114-121`) | **Fault.** Callers are silently losing vendor PVC. |
| `PVC details not found for the products …` | rsm-ws owners | **Fault, unexplained** (§9.1). |
| **Every `vendorPvcRate` is 1** | DBA — apply changeset `RQ171766-pvc-product-pvc-pkg` to that country's `pvc` schema. Check `PVCDATABASECHANGELOG` **before** raising anything (§10) | Fault, but a known one with a known fix; no code change needed. |
| PVC rates otherwise wrong or differing between countries | PVC PL/SQL owners; check `PVCDATABASECHANGELOG` per country first (§10) | Fault. |
| Wrong-country data returned | rsm-ws owners — silent datasource fallback (§11.4) | Fault. |
| 401 / credentials | Config-server owners — credentials come from Spring Cloud Config (`application.properties:25-26`) | Neither; access request. |
| Startup fails with `BeanCreationException: targetableLiquibaseInstances` | DBA + rsm-ws owners — Liquibase precondition or lock (`PvcDataSourceConfig.java:174`) | Fault. |
| Hikari pool exhaustion / connection timeouts | DBA — quote the pool name (`gb-custom` etc.), which identifies country *and* schema (§7.2) | Fault. |
| "Who changed this rebate?" | Cannot be answered from anything rsm-ws reads (§10) | Decline. |

Hand over, every time: the **`countryIdentifier`**, the **`customerAccountCode`** and/or
**`rebateSchemeCode`** and product codes, the **endpoint path and version (V1 or V2)**, and a
**timestamp window with its UTC offset** — and get it within seven days, before the log rolls
away (§7.3). Because there is no correlation id, a timestamp window plus the access log is the
only way to tie a caller's complaint to a line in `rsm-ws.log`.

Owner contacts named in the repo: `JavaDev.Support@Computacenter.com` / Commercial Systems
Development (`SwaggerConfig.java:40-42`); API spec contact `david.hinselwood@computacenter.com`
(`swagger-spec/rsm-ws-openapi.yaml:7`); SonarQube project `mutabilis:rsm-ws` (`.gitlab-ci.yml`).

## 13. Provenance and known unknowns

Verified against commit **`8031d47`** (dated 2026-05-27), on **2026-07-28**, at
`pom.xml` version `0.17.0-SNAPSHOT`.

**The working tree was not clean when this was written.** Two files carry uncommitted changes,
both affecting §7.1: `logback-spring.xml` adds `sit` to the `local` profile block (so `sit` also
gets `org.springframework` DEBUG, Hibernate SQL DEBUG, binder TRACE and console output), and
`application-sit.properties` redirects `sit` logging to `c:/temp/logs/rsm-ws/rsm-ws.log`,
switches it to `datasourceconfig-local.properties`, and replaces
`spring.liquibase.contexts=sit` with `spring.liquibase.enabled=false`. **Before trusting §7.1
or §7.3 for `sit`, confirm which version is deployed.** Nothing in §1–§6 or §8–§12 is affected.

| Section | Status |
|---|---|
| §1, §1.1, §2, §3, §3.1, §4, §5, §6 | `verified-in-code` |
| §7 — file name, config path, level-per-profile, rolling policy | `verified-in-code` |
| §7 — the pattern's exact text and the abbreviated logger names | `derived-from-config`: the pattern is Spring Boot's unmodified `FILE_LOG_PATTERN` and the abbreviations are computed from `%-40.40logger{39}`. **No line was read off a host.** |
| §7.2 | `verified-in-code` (absence of retry machinery; pool timeouts confirmed applied) |
| §8, §9, §9.1, §9.2 | `verified-in-code`. All 15 message literals were re-extracted from `git ls-files '*.java'` and compared character-for-character against this document on a second pass; all 15 match, including the space before the colon in `… software licences country {}, for customer {} : {}`. |
| §10 | `verified-in-code`, except the access log's own format/retention |
| §11, §12 | `verified-in-code` |

Known unknowns — explicit TODOs:

1. **Confirm the pattern on a host.** The one variable part is how the application name `rsm-ws`
   (`bootstrap.properties:1`) is bracketed between `---` and the thread name; this differs
   between Spring Boot 3.x and 4.x. The Spring Boot version is inherited from
   `cc-standardized-web-app-parent:4.0.3` (`pom.xml:4-8`), which **this repo does not pin and
   which did not resolve locally**. Boot 4.x is strongly indicated by the imports
   (`org.springframework.boot.tomcat.servlet.TomcatServletWebServerFactory`,
   `TomcatConfig.java:5`; `tools.jackson.databind.json.JsonMapper`, i.e. Jackson 3,
   `ProductPvcRowMapper.java:17`) but is not proven. Everything else in §7 — the ISO-8601
   timestamp with offset, `%5p` padding, the `---` separator, `[thread]`, `%-40.40logger{39}`,
   ` : `, inline `%wEx` stack traces — is identical across both versions.
2. **Read `cc-standardized-web-app`** and document `ControllerHelper.catchAndLog`,
   `StandardizedControllerExceptionHandler`, `getAuthenticationExclusionUrls()`, and the Tomcat
   access-log valve. Until then §4's HTTP statuses and all exception-log wording are unknown,
   and the access log's retention is unknown.
3. **Confirm on a host** the real `rsm-ws.log` volume per day, hence whether retention is truly
   ~7 days or less, and whether `sit`/`local`'s `c:/temp/...` paths are real Windows hosts.
4. **Confirm which callers use V1 vs V2** of the "oe" and PVC endpoints. V1 is `@Deprecated`
   (`RebateService.java:73`, `:96`) but nothing in this repo records who still calls it.
5. **Confirm whether `PVC` expands to anything** and get it into §2 rather than §2.1.
6. **Confirm `product_pvc_pkg_1.sql` and `product_pvc_pkg.sql`** under `src/db/schemas/` are
   intentionally unreferenced, or delete them (§11.11).
7. **Confirm on each of the six country databases that changeset `RQ171766-pvc-product-pvc-pkg`
   is applied.** This is the highest-value open check in this document: where it is missing,
   `vendorPvcRate` is 1 for every vendor and nothing is logged (§2, §9.1, §12). Not verifiable
   from the repo.

A refutation pass was run over this document against the repo. It found one substantive error —
citations to `product-pvc-pkg-body.sql` were ambiguous between two files of that name, one of
them superseded and containing the RQ171766 defect — which produced §2's `vendorPvc` entry, the
new §12 row, and the rewrite of §11.11. The 15 message literals, the per-level counts, the
per-profile levels, the absence of retries and writes, and the header/field casing all survived
unchanged.