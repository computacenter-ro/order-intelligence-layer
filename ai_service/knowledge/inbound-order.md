```yaml
service: "inbound-order"
aliases: ["Inbound Orders", "Inbound Order", "inbound-order-worker"]
repo: "inbound-order"
log_files: ["d:\\apps\\inbound-order\\inbound-order.log"]
log_format: "plain text"
host_retention: "SizeAndTimeBasedRollingPolicy, 10MB per file, maxHistory 7, totalSizeCap 0 (unbounded), archives gzipped — all Spring Boot defaults, nothing overrides them"
environments: ["local", "override-local", "dev", "sit", "test", "cte", "ua2", "preprod", "prod"]
correlation_id: "MDC key `EventId`, printed on EVERY line. Set on only 4 code paths (InitOrderService:65, InitOrderService:119, OrderDataReadyListener:51, OrderCreatedListener:61). NOT set by SalesforceCreateCaseListener or OrderRegenerateListener, and never cleared in a finally block — so it leaks across messages on pooled consumer threads."
primary_join_key: "eventId — the b2b_order_audit.boa_evt_id sequence value; appears both in the [EventId: …] prefix and inside message text"
statuses_written: ["custom_order_search.ord_status_cd = 'A'", "b2b_order_audit.boa_resp_sts_cd = '200' / boa_resp_sts_txt = 'Accepted'", "LISTENER_STATUS.status = RUNNING", "LISTENER_STATUS.status = STOPPED"]
```

# inbound-order

## 1. What this service does

inbound-order is the front door for commercial orders that arrive from other systems rather than from a person in Order Engine. A partner system posts an order as JSON to one HTTP endpoint; the service stores the original message verbatim as an audit record, sanity-checks it, and then answers `202 Accepted` immediately with an `eventId`. Everything real happens afterwards, asynchronously, over message queues: the service enriches the bare inbound order into a full Order Engine order — resolving the customer's linked accounts (sold-to, ship-to, bill-to, payer, end customer, sales assistant), looking up the account's settings and default warehouse, matching each order line to a catalogue item or building a non-catalogue / text / delivery line for it, applying currency conversion and default pricing — and hands the finished order to Order Engine to be saved or auto-approved.

It also handles two follow-ups. When Order Engine reports back that an order was created, this service links the new cart header id onto the audit record and, if auto-approval did not succeed for an eligible order type, raises a Salesforce case asking a human to approve it and flips the order to `Active` so it becomes visible in order search. And it can regenerate a previously rejected order from the stored original payload, replaying the whole enrichment path and producing a fresh order.

### 1.1 What inbound-order is *not* responsible for

Its responsibility ends when it publishes a message to `inbound-order-exchange` on the `/inbound_order` virtual host (QueuePublishService:111). After that:

- **Saving, approving, pricing-validating or rejecting the order** is Order Engine's. inbound-order never writes an order, cart header or cart item row.
- **Auto-approval itself** is Order Engine's. inbound-order only decides *which queue* to route to (`order.approval` vs `order.create`, OrderDataReadyListener:56-62) based on origin eligibility, the regenerate flag, and the `AUTO_APPROVAL_B2B` toggle. The `autoApprovalFailed` boolean comes back *to* this service on `order_created`.
- **Building the default-order data** (linked partner functions, default cart items, default cart header) is Order Engine's — it arrives already assembled in the `order_data_ready` payload (AmqpDefaultOrderData).
- **User rights and privileges** — resolved from JAM (JamService:26) and Settings WS; this service only reads them.
- **The Salesforce case lifecycle** after creation. This service creates the case and stores the returned case number as the `SFCaseNumber` attribute (SalesforceService:154-159). Working the case is a human/Salesforce concern.
- **Order search screens.** This service writes one column of `custom_order_search` and nothing else.

## 2. Key concepts

| Term | What it actually means, verified in code |
|---|---|
| **eventId** | The primary key of the `b2b_order_audit` row created for the inbound message (`boa_evt_id`, sequence `SEQ_BOA`, B2BOrderAuditEntity:22-25). It is the MDC `EventId`, the `metadata.eventId` on every queue payload, and the `eventId` returned to the HTTP caller. |
| **Audit record** | `martini_custom.b2b_order_audit`. `boa_inb_msg_dta` holds the complete original JSON request as a LOB (B2BOrderAuditService:84). Regenerate and Salesforce attachments both re-read that column. |
| **Order origin** | `OrderOriginEnum` — two different strings per value: a `sourceSystem` sent by the caller and an `oeType` used internally. They differ for two values: `SnD`/`SND` and `SF`/`VCT` (OrderOriginEnum:10-15). |
| **Eligible for auto-approval** | Exactly B2B, SND, HYB, SNOW (OrderOriginEnum:55-57). This same flag also gates whether a Salesforce approval case is raised on auto-approval failure (OrderCreatedListener:101). |
| **isB2BOrigin** | A *different, narrower* set: B2B, SND, SNOW — HYB is excluded (OrderOriginEnum:59-61). Do not treat the two as interchangeable. |
| **Regenerate** | Rebuild an order from the stored audit payload. Triggered by a message on `order_regenerate` carrying a bare cart header id string (OrderRegenerateListener:39). XML payloads are refused (InitOrderService:121-124). |
| **Country code** | Derived purely from the first two characters of the sold-to account number via a hardcoded map; unknown prefixes give `XX` (CountryService:17-46). It is not read from the request. |
| **`SFCaseNumber`** | The attribute name under which a created Salesforce case number is stored against the cart header in `martini_store.object_attribute` (SalesforceService:39, ObjectAttributeService:31). |
| **Listener** | One of four named Rabbit consumers (ListenerEndpointId:15-18), individually startable/stoppable over `/admin/**` and with state persisted in `MARTINI_CUSTOM.LISTENER_STATUS`. |
| **Non-catalogue / text / delivery line** | Three fallbacks when an order line cannot be matched to exactly one predefined cart item. Signalled internally by `BusinessException.ErrorCode` values `TEXT_ITEM_FOUND`, `DELIVERY_ITEM_FOUND`, `NON_CATALOG_ITEM_CREATION` (SearchCartItemService:59-90) — these are *routing decisions, not failures*. |

### 2.1 Concepts this document does not cover

Decline rather than infer on these — the meaning could not be pinned to a line of code in this repo:

- **What each Order Engine order status means**, and what `autoApprovalFailed = true` implies for the order downstream. This service reads the flag; it does not define it.
- **Whether the queues declared in `AmqpQueueConfig` are actually created at runtime by this application.** Both `RabbitAdmin` beans are built with `setAutoStartup(false)` (AmqpBrokerConfig:89, 96), and the local broker is pre-provisioned from `docker-rabbit/definitions.json`. Which component declares them in dev/sit/prod is unverified.
- **Which `spring.application.name` reaches the `LISTENER_STATUS.application` column.** `bootstrap.properties:1` says `inbound-order-worker`; `application.properties:1` says `inbound-order`; the column is filled from `${spring.application.name:inbound-order-worker}` (application.properties:183). Read the table rather than predicting it.
- **Whether the three `…FallbackFactory` classes ever execute.** They are named on `@FeignClient` annotations, but there is no `spring-cloud-starter-circuitbreaker*` dependency in `pom.xml` and no `spring.cloud.openfeign.circuitbreaker.enabled` property anywhere. Treat their three ERROR lines as possibly unreachable.
- **`ZBUN`, `ZORM`, `NORM`, `CPD customer`, `Engineer in a Box`, `rebate sub-scheme`** — these appear as codes and flags but their business meaning is defined elsewhere.
- **The `mail.support.notification.from-no-reply` value** — supplied by external CSD Cloud Config, not in this repo.

## 3. How work flows through this service

**Path A — new order (HTTP + 3 queue hops)**

1. `POST /api/v1/create` → `InboundOrderController:31` → `InitOrderService.createOrder` (`@Transactional`).
2. Order lines are sorted by `orderLineNumber` (InitOrderService:61-62); the audit row is created and flushed, yielding the `eventId` (InitOrderService:63-65).
3. Country code derived from the sold-to account number (InitOrderService:67-68).
4. All `Validator` beans run and their findings are collected — see §4.
5. On success: enrichment request built (InitDataRequestProcessor:26), a JWT minted for the order's `sfOwnerEmployeeNumber` (InitOrderService:89), published to `order.init`, and `202 Accepted` returned.
6. Order Engine assembles default order data and publishes `order_data_ready`.
7. `OrderDataReadyListener` → `OrderDataService.buildSaveOrderRequest`: re-read the audit payload, fetch account settings, then **header → partner functions → cart items** in that order (OrderDataService:47-55). Header must run first because partner functions and cart items both consume values it sets (currency code, cart header attributes).
8. Route to `order.approval` if the origin is auto-approval-eligible **and** not a regenerate **and** the `AUTO_APPROVAL_B2B` toggle is on; otherwise `order.create` (OrderDataReadyListener:56-62, 66-70).

**Path B — Order Engine reports back.** `OrderCreatedListener:57` branches three ways and *returns early* on the first two, so the branches are mutually exclusive:

- auto-approval succeeded (`!autoApprovalFailed`) → save the cart header id onto the audit, done (OrderCreatedListener:71-74).
- else if this was a regenerate event → duplicate the audit row under the new cart header id (with a fresh random `messageReference`, B2BOrderAuditService:95) and publish `order.regenerated.ready` (OrderCreatedListener:76-79, 113-121).
- else → if the origin is auto-approval-eligible, raise a Salesforce approval case and set `custom_order_search.ord_status_cd` to `'A'`; then save the cart header id (OrderCreatedListener:98-111).

**Path C — regenerate.** `order_regenerate` (payload: a bare cart header id string) → find the audit by cart header id → reject non-JSON payloads → rebuild the init request with `isRegenerateOrderEvent = true` → publish `order.init`, rejoining Path A at step 6.

**Path D — Salesforce case retry.** `salesforce_create_case` → create the case → store `SFCaseNumber` against the cart header found by order number (SalesforceCreateCaseListener:42-44). **Nothing in this service publishes to `salesforce.create.case`** — only to `salesforce.create.case.error` (QueuePublishService:98). This listener exists to consume messages replayed back from the error queue by an operator.

### 3.1 Why a step gets skipped

- **A listener is stopped.** Messages sit in the queue, nothing is logged, nothing errors. Check `GET /admin/status` (live container state, not the DB — ListenerLifecycleService:140) and `MARTINI_CUSTOM.LISTENER_STATUS`. A listener whose row has `autoStart = false` is *deliberately* left stopped at boot (ListenerLifecycleService:43); the column is expected to be set by manual DB update.
- **Auto-approval routing.** All three conditions must hold (§3 step 8). If `AUTO_APPROVAL_B2B` has no row in the toggle table, `isFeatureEnabled` returns `false` (AppFeatureToggleService:22) — a *missing* toggle silently disables the feature.
- **No Salesforce case on approval failure** when the origin is VCT or CPQ — they are not auto-approval-eligible, so `handleAutoApprovalFailure` skips both the case and the `'A'` status flip (OrderCreatedListener:101-108).
- **Order stays invisible in order search.** The flip to `'A'` happens *only* on the auto-approval-failure branch. Success and regenerate branches return before reaching it.
- **No cart header id on the audit** if `headerId` is empty or the audit row cannot be found (OrderCreatedListener:124).
- **Regenerate silently does nothing** if no audit row exists for that cart header id — `orElseThrow()` raises `NoSuchElementException` (InitOrderService:117), which is not caught locally and ends on the error queue.
- **A cart item is not matched.** Any of: key resolves to a text/delivery marker, no entry for the key, zero survivors after origin filtering, or *more than one* survivor — all four produce a non-catalogue-style line instead (SearchCartItemService:59-90). "Multiple matches" is not an error; it falls back.

## 4. The rules that stop something

Four `Validator` beans run on the HTTP path only (OrderSanitizationValidatorService:27-30). All of them run to completion and their errors are pooled — one failure does not short-circuit the others.

**4.1 Sold-to must exist — BUSINESS** (OrganizationValidator:34-54). Fails if the sold-to postal address number is missing/empty or no organization exists for it. This error is privileged: if it is present, `InitOrderService:73-78` throws immediately with *only that error* and **no Salesforce case is raised**. Code `CC_ERR_ECI_INVALID_SOLD_TO`. Message, per locale: `The Sold To account number for the order is invalid.` (en) / `Die Kundennummer des Auftraggebers ist für den Auftrag ungültig` (de_DE, note: no trailing period) / `Le numéro de client du donneur d'ordres n'est pas valable pour la commande.` (fr_FR) / `Het Verkocht aan accountnummer voor de order is ongeldig` (en_NL). The de_DE and fr_FR texts are quoted here as the file *bytes* decode under ISO-8859-1 — see the encoding trap in §11 for what is actually emitted at runtime.

**4.2 Customer order reference must be present — BUSINESS** (CartHeaderValidator:31-46). Code `CC_ERR_ECI_MISSING_CUSTOMER_ORDER_REFERENCE`, message `The customer order reference is missing` (en, no trailing period) / `Die Kundenauftragsreferenz fehlt` (de_DE) / `La référence de commande client manque.` (fr_FR) / `De klant orderreferentie ontbreekt` (en_NL).

**4.3 Address field lengths — BUSINESS** (AddressFieldsValidator:51-95). Checked on all six partner-function addresses. Limits in code: name1 40, name2 40, name3 128, name4 80, street 80, street4 80, district 80, city 40, postcode 40. **The user-facing text for `ADDRESS_NAME2_TOO_LONG` disagrees with the code in every bundle**: `The name2 field exceeds the maximum length of 35 characters.` while `MAX_ADDRESS_NAME2_LENGTH = 40` (AddressFieldsValidator:30). All nine address messages are English-only in all five bundles.

**4.4 Order line quantity and unit of measure — BUSINESS** (CartItemsValidator:34-64). Missing quantity → `INVALID_QUANTITY`, `Missing quantity for order line.` (en). Unit of measure is checked **only when `validation.unit-of-measure=true`; the shipped value is `false`** (application.properties:99), so `INVALID_UNIT_OF_MEASURE` (`Invalid unit of measure for order line.`) is effectively dormant.

**What happens on failure:** if any error other than invalid-sold-to is present, a Salesforce `REJECTED_MESSAGE` case is created with all the messages joined by newlines as the description, and *then* a `ValidationException` is thrown (InitOrderService:79-85) → HTTP 400 carrying the **first** error only (ControllerExceptionHandler:35-41).

**4.5 Invalid order origin — FAULT, and it is on the queue path, not the HTTP path** (DelegateOrderHeaderProcessor:50-59). If the cart header's `orderOrigin` maps to no `OrderOriginEnum`, a `ValidationException` is thrown with the literal message `Invalid order origin`, inside `OrderDataReadyListener` — so it goes to `order_data_ready_error`, not back to the caller.

**4.6 Ship-to missing or invalid — BUSINESS** (B2BShipToPartnerFunctionStrategy:110-122). Creates a Salesforce `REJECTED_MESSAGE` case with subject `PO - <customerOrderReference>` and reason `Invalid ship to account.` (`INVALID_SHIP_TO`, English-only in all bundles). At commit `22bae2d` it then throws `OrderValidationException("Ship to address is mandatory")`, which the error handler treats specially — see §9.1. **Note:** if no sold-to account is present in the linked partner functions, the whole block is skipped and neither the case nor the exception happens (the `.ifPresent(...)` at :113).

**4.7 No currency conversion rate — FAULT** (ConversionService:87-93). `IllegalArgumentException` with `Could not find conversion rate from %s to %s`.

**4.8 Unsupported queue type — startup FAULT** (AmqpQueueConfig:185-188). `BeanInitializationException`: `Invalid queue type specified [%s], allowed types are %s`. Only `classic` and `quorum` are allowed (AmqpHeaderConstants:41). The application will not start.

## 5. What users can do

There is **no user interface**. Two HTTP surfaces, both HTTP Basic, both stateless (WebSecurityConfig:137-147):

| Surface | Path | Required authority | Who calls it |
|---|---|---|---|
| Create order | `POST /api/v1/create` | `inbound_order` | partner systems (B2B, SnD, HYB/TechSource, SF/VCT, SNOW, CPQ) |
| Listener admin | `GET /admin/{start,stop}/{id}`, `/admin/{start,stop}/all`, `/admin/status` | `listener_admin` | CSD Admin Tool |

Two in-memory users exist (WebSecurityConfig:80-96): the inbound-order user holds **both** authorities; the listener-admin user holds only `listener_admin`. Credentials come from `inbound.order.api.security.*`, jasypt-encrypted per profile.

Regenerating an order is **not** an endpoint — it is triggered by publishing a cart header id to `order_regenerate`. The README's claim of "two endpoints" is stale; the OpenAPI spec defines exactly one path (`swagger-spec/inbound-order-openapi.yaml:10`).

Causes of "I can't do X", in priority order:

1. **401 with `Not authorized`** — wrong Basic credentials or wrong authority for the path (WebSecurityConfig:109-115). Nothing is logged.
2. **400 with no log line at all** — malformed JSON (`HttpMessageNotReadableException`) and bean-validation failures on the request body both return 400 from handlers that contain **no logging statement** (ControllerExceptionHandler:65-87). This is the most confusing silent case: to the caller it looks like the service ignored them.
3. **400 from business validation** — §4. The validators *do* log at ERROR, so these are greppable.
4. **A stopped listener** — accepted with 202, then nothing. Silent by design; see §3.1.
5. **404 from `/admin/…`** — the id is not one of the four values in `ListenerEndpointId` (ListenerController:42, ListenerEndpointId:40-46). Not a failure, just an unknown name.
6. **Swagger UI unavailable in prod** — `SwaggerConfig` is `@Profile("!prod & !junit")`, and `springdoc.api-docs.enabled=false` globally.

## 6. Statuses, and who writes them

| Value written | Where | Written by |
|---|---|---|
| `'A'` (`StatusCode.ACTIVE`) | `martini_store.custom_order_search.ord_status_cd` | CustomOrderSearchService:35, reached only from OrderCreatedListener:107 |
| `'200'` / `'Accepted'` (`RequestStatus.SUCCESS`) | `martini_custom.b2b_order_audit.boa_resp_sts_cd` / `boa_resp_sts_txt` | B2BOrderAuditService:85-86 (new audit) and :98-99 (duplicated audit) |
| `RUNNING` | `MARTINI_CUSTOM.LISTENER_STATUS.status` | ListenerLifecycleService:46, 71, 87 |
| `STOPPED` | `MARTINI_CUSTOM.LISTENER_STATUS.status` | ListenerLifecycleService:107, 123; ListenerStatusService:73-78 (bulk, on shutdown) |

**This service writes exactly four distinct status values across three tables.** Two enum members are defined but **never written**: `StatusCode.INACTIVE` (`'I'`) and `RequestStatus.LOAD_FAILURE` (`'444'` / `'CC_ERR_ECI_LOAD_FAILURE'`) — grepping for writes finds no caller. If a support agent sees `'I'` or `'444'`, another application put it there.

---

## 7. Log anatomy — read this before quoting any line

Config: `src/main/resources/logback-spring.xml`. File path: `logging.file.name=d:/apps/inbound-order/inbound-order.log` (application.properties:21); local profiles override to `c:/apps/…`.

The file pattern, character for character (logback-spring.xml:8-9):

```
-%d{-yyyy-MM-dd HH:mm:ss.SSS} -%5p ${PID:- } [EventId: %X{EventId}] --- [%t] %-40.40logger{39} : %m%n-%wEx
```

Parsing hazards, all following directly from that string:

- **Every line starts with a doubled hyphen.** There is a literal `-` before `%d`, and the date format itself begins with another literal `-`.
- **The level is hyphen-prefixed and right-padded to 5.** So you grep for `-DEBUG`, `-ERROR`, `- INFO`, `- WARN` — note the space inside the four-letter ones.
- **No timezone or offset is recorded.** Timestamps are host local time. Correlating with another service means knowing the host's zone.
- **`[EventId: ]`** with nothing between the colon-space and `]` means no MDC value was set on that thread — see §11.
- **Thread name is untruncated** (`[%t]`), so its width varies; the logger name is left-justified in a fixed 40-column field, abbreviated to at most 39 characters by collapsing leading package segments to single letters left-to-right. The **class simple name always survives**, so grep on `OrderCreatedListener`, not on the full package.
- **Stack traces are preceded by a lone `-` line.** `%m%n-%wEx` emits the message, a newline, a literal `-`, and only then the whitespace-wrapped throwable. A multi-line join rule must treat both the `-` line and everything after it as continuation until the next line starting `--<digit>`.
- `<configuration scan="true">` plus `<jmxConfigurator/>` (logback-spring.xml:2-3) mean **levels can be changed at runtime** and the config file is re-read periodically. The level in the file may not match the level in `logback-spring.xml`.

Worked example (field values illustrative, shape exact):

```
--2026-07-29 15:17:03.412 - INFO 4812 [EventId: 4471903] --- [http-nio-8096-exec-3] c.c.i.service.InitOrderService           : Inbound order responding with : CreateOrderResponseDto(…) 
```

Console output exists **only** under the `local` / `override-local` profiles (logback-spring.xml:17-29) — everywhere else the FILE appender is the sole destination.

### 7.1 Level availability per environment

| Profile | `com.computacenter.inboundorder` | `org.springframework` | `org.hibernate` / `org.hibernate.SQL` |
|---|---|---|---|
| local, override-local | DEBUG (+console) | INFO (+console) | not set |
| dev, sit | DEBUG | INFO | ERROR |
| test, preprod, cte, ua2 | DEBUG | INFO | ERROR |
| **prod** | **INFO** | **not set — only `org.springframework.web` is, at ERROR** | ERROR |

Root is `INFO` with the FILE appender (logback-spring.xml:14-16). In prod the whole of `org.springframework` other than `.web` therefore falls through to root INFO — do not claim "Spring is at ERROR in prod".

Statement counts, from `git grep` over `src/main/java/*.java` at commit `22bae2d`, excluding commented-out lines:

| Level | Count |
|---|---|
| TRACE | 0 |
| DEBUG | 34 |
| INFO | 12 |
| WARN | 5 |
| ERROR | 23 |

**This is the prod-visibility story: 34 of the 74 statements — every per-order milestone, every payload dump, the whole cart-item and partner-function resolution trail — are DEBUG, and prod runs this package at INFO.** In prod you get 12 INFO, 5 WARN and 23 ERROR statements and nothing else. Reconstructing *why* an order was enriched a particular way is not possible from a prod log.

### 7.2 Retries

**AMQP: there is no retry.** The container factory sets a connection factory, converter, error handler and concurrency and nothing else — no advice chain, no `RetryTemplate` (AmqpBrokerConfig:100-112). A listener failure is handled once by `CustomRabbitListenerErrorHandler` and the message goes to the error queue. **One `Exception occurred during message processing…` ERROR line equals exactly one logical failure — never collapse repeats as retries of the same message.** `inbound.order.amqp.max-retries-number` exists as a field on `AmqpConfigProperties` but no property sets it and no code reads it: dead config.

**Feign clients** use `Retryer.Default(initialDelay, maxDelay, maxAttempts)` (FeignClientBasicConfig:37):

| Client | Attempts | Initial / max backoff |
|---|---|---|
| Settings WS | 4 (`default.retry.attempts`) | 200 ms / 3000 ms |
| JAM | 4 (`default.retry.attempts`) | 200 ms / 3000 ms |
| Salesforce | 3 (`sf.api.retry.attempts`) | 200 ms / 3000 ms (inherited) |

**Only HTTP 5xx retries.** `FeignClientErrorDecoder:32-40` wraps status ≥ 500 in `RetryableException`; every other error status becomes `NonRetryableException` and fails on the first attempt. **Timeouts are live, not dead config:** `connectTimeout=10000` and `readTimeout=20000` (application.properties:80-81) apply because no client configuration class defines a `Request.Options` bean.

**Dead retry config to ignore:** `sf.add.attachment.attempts=3` and `sf.add.attachment.delay=5000` (application.properties:97-98) — nothing reads either. Likewise `object.attribute.location.id = 1` (application.properties:94).

**Errors that appear exactly once per occurrence** (no retry loop above them): everything in `CustomRabbitListenerErrorHandler`, both `Create Salesforce case request failed: {}` variants, and every validator ERROR.

### 7.3 Retention and file names

Live file `d:\apps\inbound-order\inbound-order.log`. Archives `d:\apps\inbound-order\inbound-order.log.<yyyy-MM-dd>.<i>.gz`. Rolls at 10 MB or daily, keeps 7, unbounded total size, does not clean on start — all Spring Boot `file-appender.xml` defaults, since no `logging.logback.rollingpolicy.*` property appears in any profile. Tomcat access logs go to a separate directory, `d:/apps/inbound-order` (`cc.tomcat.accesslogvalve.directory`, application.properties:22).

## 8. Identifiers in the logs

| Identifier | How it appears in log text | Scope |
|---|---|---|
| **eventId** | in the prefix as `[EventId: 4471903]`, and in text as `…for event id: 4471903 and header id: …` | **the cross-service join key** — same value in the audit table PK, the queue payload `metadata.eventId`, and the HTTP response |
| cart header id | `and header id: {}`, `with headerId {}`, `with cartHeaderId {}` | Order Engine's cart header — cross-service |
| order source number | `order with number {}`, `The new regenerated order with number {} is created` | Order Engine order number — cross-service |
| account number | `and account number: {} ` | sold-to account — cross-service |
| correlationId | `Check error queue order_created_error for correlationId 3f2a…` | **local to one failure** — a fresh `UUID.randomUUID()` per failure (CustomRabbitListenerErrorHandler:97). Also set as the AMQP correlation-id on the error-queue message and quoted in the failure email. Not a join key to any other service. |
| Salesforce case number | `Salesforce case was successfully created with number {}` | cross-service to Salesforce |

**`messageReference` never appears in any log line.** It is in the request, the audit row, and the Salesforce case, but you cannot grep the log for it — translate it to an `eventId` via `b2b_order_audit.boa_inb_msg_ref` first.

## 9. Journey stages and their log evidence

Quoted exactly, with level and emitting class.

**Accepted at the front door**
- DEBUG `InitOrderService` — `Request payload : {}` (the entire pretty-printed inbound order)
- DEBUG `InitOrderService` — `Validation failed for incoming order. Messages: {}`
- INFO `InitOrderService` — `Inbound order responding with : {} ` *(space before the colon, trailing space)*
- DEBUG `QueuePublishService` — `Send init order data event with payload : {}` then `Event sent to routing key {}.` *(trailing full stop)*

**Enrichment**
- DEBUG `OrderDataReadyListener` — `Init Order Data event received from application name: {} with payload {}`
- INFO `OrderDataReadyListener` — `Order Data Ready Listener received Save order request for event id: {} ` *(trailing space)*
- DEBUG `OrderHeaderService` — `For order with event id : {} header data values processed {}`
- DEBUG `PartnerFunctionService` — `For order with event id : {} was found {} accounts with account numbers {}`
- DEBUG `CartItemService` — `For order with event id : {} was found products with part numbers {}`
- DEBUG `QueuePublishService` — `Send order auto approval event with payload : {}` **or** `Send create order event with payload : {}`

**Cart-item resolution** — five DEBUG lines from `SearchCartItemService`, all keyed on `key`, and **their wording is not uniform**:
`Text item number was found for key {} so there will be created a text item` · `Delivery item number was found for key {} so there will be created a delivery line` · `No cart item found for key {} so there will be created a non catalog item` · `After filtering multiple order lines for key {}, no conditions were met so there will be created a non catalog item` · `After filtering multiple order lines for key {}, still there are multiple items and we just create the default non catalog item` (this last one is split across two source lines, :86-87) · plus the success case `There is only one cart item for key {} so we just use it`.

**Order Engine reports back** — all `OrderCreatedListener`
- DEBUG `Created order event received from application: {} with payload {}`
- INFO `Notification for Order Successfully Created received for order with event id: {} and header id: {} and account number: {} ` *(trailing space)*
- DEBUG `Auto approval process is done for order with number {}` — the success branch
- DEBUG `The new regenerated order with number {} is created` — the regenerate branch
- DEBUG `After case creation, customOrderSearch with headerId {} will change status to Active so it can be visible on search main page`
- DEBUG `Order creation process is done for order with number {}` — the failure branch

**Regenerate**
- INFO `OrderRegenerateListener` — `Regenerate Order event received from application: {} with cartHeaderId {}`
- DEBUG `InitOrderService` — `Request on order regenerate with original payload : {}`

**Salesforce**
- DEBUG `SalesforceService` — `Case will be created for order with number {}`
- INFO `SalesforceService` — `Salesforce case was successfully created with number {}`
- ERROR `SalesforceService` — `Create Salesforce case request failed: {}` — **emitted from two different lines** (:64 for a non-SUCCESS response, :71 for a thrown exception). Only the :71 call passes the exception, so only that one carries a stack trace.
- DEBUG `SalesforceCreateCaseListener` — `Salesforce case create event received from application name: {} with payload {}`

**Failure handling**
- ERROR `CustomRabbitListenerErrorHandler` — `Exception occurred during message processing. Check error queue {} for correlationId {}. Exception: ` *(trailing space before the appended stack trace)*
- INFO `OrderAmqpFailureMailService` — `AMQP failure notification email sent for correlationId={}, destinationQueue={}`
- ERROR `OrderAmqpFailureMailService` — `Failed to send AMQP failure notification email for correlationId={}, destinationQueue={}: {}`
- WARN `OrderAmqpFailureMailNotifier` — `Could not serialize diagnostic headers for failure notification: {}`
- The three `NotifyOnAmqpFailureAspect` WARNs are a **family with three distinct texts** — do not search for one and assume you have them all: `NotifyOnAmqpFailure (Salesforce): unexpected join point {}, skipping notification` · `NotifyOnAmqpFailure (listener): unexpected join point {}, skipping notification` · `NotifyOnAmqpFailure (listener): missing Spring message in {}, skipping notification`

**Downstream client failures** — these come from the **client configuration/fallback classes, not from the service classes**: `SettingsClientFallbackFactory` (`Error while calling settings for company: {} with salesforce number: {} for account number: {}`), `JamClientFallbackFactory` (`Error while calling JAM when loading privileges for payroll number {}`), `SalesforceDataClientFallbackFactory` (`Error while calling Salesforce case creation with request {}. Cause: {}` and `Error while calling sales force to search contacts with cause: `). All ERROR. See §2.1 on whether they are reachable. `JamService:29` emits its own separate ERROR, `Error while calling JAM with payroll number: {}`, and this one *is* reachable — it catches `NonRetryableException`.

**Lifecycle** — `ListenerLifecycleService` / `ListenerStatusService`, all INFO: `The application is initializing, starting eligible RabbitMQ listeners.` · `Application is shutting down, changing RUNNING listener statuses to STOPPED.` · `Starting RabbitMQ listener: {}` · `Stopping RabbitMQ listener: {}` · `{} listeners have been set to status STOPPED`

### 9.1 Lines that look like errors but are not

- **WARN `CustomRabbitListenerErrorHandler` — `OrderValidationException occurred for eventId = {}, stopping further processing. Exception: {}`.** This is the *intended* outcome of a business rejection (currently only the ship-to rule, §4.6). The handler returns `null`, the message is **not** forwarded to an error queue, and **no failure email is sent** (NotifyOnAmqpFailureAspect:73). Do not raise a ticket for this line — the Salesforce case has already been created and a human needs to act.
- **ERROR `AddressFieldsValidator` / `CartHeaderValidator`** log the raw English `ErrorEnum` message with no context and no placeholders (`log.error(errorEnum.getErrorMessage())`, AddressFieldsValidator:92). A bare line reading `The city field exceeds the maximum length of 40 characters.` is a rejected customer address, not a fault.
- **ERROR `OrganizationValidator` — `Order request with empty Sold To address.`** and `Organization does not exist for account number: <n>` (string concatenation, no `{}`). Customer data problems.
- **ERROR `DateMapper` — `Unsupported date format: {}`** and **ERROR `B2bShippingAndDeliveryProcessor` — `Unsupported date format: {} . Delivery date and time not set.`** (note the space before the full stop). Near-identical prefixes from two different classes; both are tolerated — the first returns `null`, the second just leaves the delivery date unset. Neither stops the order.
- **The three `BusinessException` codes** never reach a log as an error — they are caught and turned into a non-catalogue/text/delivery line (CartItemService:120-124).

### 9.2 Queue / topology reference

**Two virtual hosts.** Publishing goes to `/inbound_order`; three of the four listeners consume from `/order_engine` (AmqpBrokerConfig:27-48).

| Direction | Queue | Exchange / routing key | Error queue |
|---|---|---|---|
| **in** | `order_data_ready` | `/order_engine` | `order_data_ready_error` |
| **in** | `order_created` | `/order_engine` | `order_created_error` |
| **in** | `order_regenerate` | `/order_engine` | `order_regenerate_error` |
| **in** | `salesforce_create_case` | `inbound-order-exchange` / `salesforce.create.case` | `salesforce_create_case_error` |
| **out** | `order_init` | `inbound-order-exchange` / `order.init` | `order_init_error` |
| **out** | `order_create` | `inbound-order-exchange` / `order.create` | `order_create_error` |
| **out** | `order_approval` | `inbound-order-exchange` / `order.approval` | `order_approval_error` |
| **out** | `order_regenerated_ready` | `inbound-order-exchange` / `order.regenerated.ready` | `order_regenerated_ready_error` |
| **out** | `salesforce_create_case_error` | `inbound-order-exchange` / `salesforce.create.case.error` | — |

The three inbound `/order_engine` queues and their bindings are Order Engine's to document; the `salesforce.create.case` producer is not in this repo (§3, Path D).

**The dead-letter wiring is the opposite of what the names suggest.** Each main queue is declared with `x-dead-letter-exchange` set to the **empty string** (the default exchange) and `x-dead-letter-routing-key` set to its own error queue name (AmqpQueueConfig:34-35, 62-63, 90-91, 119-120, 147-148). The fanout exchange `inbound-order-error-dlx` is **not** attached to any main queue — it is bound *to the four order error queues* (AmqpQueueConfig:53-56, 81-84, 110-113, 138-141). `salesforce_create_case_error` is bound to the **topic** exchange with routing key `salesforce.create.case.error` and is **not** bound to the fanout.

All queues are durable, non-exclusive, non-auto-delete (AmqpQueueConfig:178). Queue type is `quorum` by default (application.properties:66), overridden to `classic` in `dev`, `preprod`, `local` and `override-local`. Consumer concurrency 3–10 (application.properties:64-65). `inboundRabbitTemplate` is set `mandatory(true)` (AmqpBrokerConfig:56). **No listener consumes any `*_error` queue** — messages accumulate there until an operator replays them.

## 10. Evidence that is not in the log file

**Error-queue message headers.** Names are **lowercase in the message** even though the Java constants are upper-case (AmqpHeaderConstants:15-33):

| Header | Content |
|---|---|
| `x-cc-failure-message` | JSON. **Two different shapes for the same header:** `{"exceptions":["msg1","msg2"]}` (an array, from CustomRabbitListenerErrorHandler:113-114) or `{"exceptions":"msg"}` (a single string, from SalesforceCreateCaseErrorHeadersFactory:53-57). For bean-validation failures it is instead a field→message map. |
| `x-cc-failure-exception` | simple class name of the innermost cause |
| `x-cc-failure-stacktrace` | newline-joined stack trace (a `StackTraceElement[]` in the Salesforce case) |
| `x-cc-received-routing-key` | the routing key the message arrived on |
| `x-cc-failure-timestamp` | ISO-8601 local date-time, no zone |
| `x-cc-producer` | `INBOUND_ORDER` or `ORDER_ENGINE` |
| `x-cc-authentication` | **⚠ a signed JWT.** The error handler adds headers to the existing properties and never strips this one, so every dead-lettered order message carries a live bearer token. Redact when quoting; treat the error queues as secret-bearing. |

Also on the message: `correlationId` and `replyTo` (set to the error queue name, CustomRabbitListenerErrorHandler:129).

**Tables.**

| Table | Kind | What this service does to it |
|---|---|---|
| `martini_custom.b2b_order_audit` | **append-only history** (one row per inbound message, one extra row per regenerate) | writes; `boa_inb_msg_dta` holds the full original payload — **customer PII** |
| `martini_store.custom_order_search` | **current-state snapshot** | writes `ord_status_cd` only |
| `MARTINI_CUSTOM.LISTENER_STATUS` | **current-state snapshot**, one row per (application, node, port, listener) | writes `status`, reads `autoStart` |
| `martini_store.object_attribute` | versioned, but this service always writes `version = 1` (ObjectAttributeService:16, 43) | writes the `SFCaseNumber` value |

**Secondary streams:** the Tomcat access log (separate directory, §7.3); the AMQP failure email; and the Salesforce case, whose Base64 attachment is the original order converted to XML (CreateCaseMapper:71-82).

**The AMQP failure email** goes to `product.and.salessystemssupport@computacenter.com` by default (OrderAmqpFailureMailProperties:26), is enabled by default (application.properties:91) and disabled only in `override-local`. Its body embeds application name, event id, broker host list, routing key, destination queue, correlation id, failure message and the diagnostic-header JSON — stack trace truncated at 2000 characters in the JSON (OrderAmqpFailureMailNotifier:42) and 4000 in the body fields (OrderAmqpFailureMailService:22).

## 11. Blind spots and traps

- **`EventId` leaks between messages.** No `MDC.remove` is inside a `finally` anywhere. `OrderCreatedListener` returns early at :74 and :79, skipping the removal at :82. `OrderDataReadyListener:63` is skipped if enrichment or publishing throws. `InitOrderService:119` sets the value and never removes it. On a pooled consumer thread the previous message's `EventId` therefore stays stamped on subsequent lines until the next `MDC.put`. **Never trust `[EventId: …]` alone — confirm against an id inside the message text.**
- **`SalesforceCreateCaseListener` never sets `EventId` at all.** Its lines carry whatever the thread last held.
- **`OrderRegenerateListener` only *removes* `EventId`** (:47) — it never puts one; the value comes indirectly from `InitOrderService:119`.
- **MDC is not propagated to the mail executor.** `AsyncNotificationConfig` copies nothing, so both `OrderAmqpFailureMailService` lines carry an empty `EventId`. Separately, the **email's own Event ID field shows `-` for Salesforce-case failures**, because `AmqpSalesforceCase` is not one of the five payload types `eventIdFromPayload` recognises (OrderAmqpFailureMailNotifier:112-117).
- **"Who did this?" cannot be answered.** `@EnableJpaAuditing` is on (JpaAuditingConfiguration:10) but **no `AuditorAware` bean exists**, and none of the four tables this service writes has a created-by or modified-by column — only `@LastModifiedDate` timestamps. Decline attribution questions.
- **Two 400 responses log absolutely nothing** — malformed JSON and request-body bean-validation failures (§5, cause 2). Failures that produce state but no log.
- **`log.error("Product with code {} not found in database", productRepo)`** (ProductService:31) passes the **repository bean**, not the product code. The line prints a Spring proxy's `toString()` where a support agent will expect a part number. The sibling line at :48 is correct.
- **`ListenerLifecycleService:40`** passes `event` with no `{}` placeholder — the argument is silently dropped. And its message says "STOPPED" (`Application is shutting down, changing RUNNING listener statuses to STOPPED.`) while the method's own Javadoc says SHUTDOWN; `STOPPED` is what is written.
- **Secrets and PII at DEBUG.** `JwtTokenService:75` logs a complete signed JWT (`Generated jwt: {} `). Every `QueuePublishService` and listener DEBUG line dumps a full order payload — names, addresses, prices. `InitOrderService:57` dumps the entire request. In non-prod (DEBUG) these are all present in the file; prod runs at INFO, which is the only thing keeping them out. Redact before pasting any DEBUG line anywhere.
- **Country-code → message-bundle lookup is not what it appears.** `getMessageForCountry` builds `new Locale("", countryCode)` — empty language, country only (MessageProvider:27). The bundles on disk are `messages_en`, `messages_de_DE`, `messages_en_BE`, `messages_en_NL`, `messages_fr_FR`; none is named for a bare country. Which text a German or French caller actually receives should be verified on the host, not assumed from the bundle files.
- **`spring.messages.use-code-as-default-message=true` does not apply.** `ApplicationConfig:27-34` defines its own `ResourceBundleMessageSource` and never calls `setUseCodeAsDefaultMessage`, so Boot's property is not in play for these lookups.
- **The German and French bundles are encoded ISO-8859-1 but read as UTF-8.** `file(1)` reports `messages_de_DE.properties` and `messages_fr_FR.properties` as ISO-8859 text (the other three are pure ASCII), and neither is valid UTF-8, yet `ApplicationConfig:31` sets `setDefaultEncoding("UTF-8")`. Every accented character in those two bundles — `für`, `ungültig`, `numéro`, `référence`, `beigefügt` — will therefore decode to a replacement character at runtime. Quote §4's German/French text as *intended* wording, and expect mojibake in anything a customer actually receives.
- **`en_BE` and `en_NL` have empty values for all eight notification subject/content keys.** Any message resolved from them yields an empty string.
- **A whole family of message keys is dead.** `NotificationType` carries `subjectMessageKey`/`contentMessageKey`, but `getSubjectMessageKey`/`getContentMessageKey` are never called anywhere. `MANUAL_APPROVAL_REQUIRED` (Constants:5) is unused, and `ERROR_FOR_MANUAL_APPROVAL_TEXT` is looked up nowhere in tracked code. **This service does not send approval/rejection notification emails** — the only email it sends is the AMQP failure alert. Do not tell anyone otherwise on the strength of the bundle contents. `CC_ERR_INVALID_CART_ITEM` is defined in `ErrorEnum` but exists in **no** bundle and is never used.
- **Audit row and HTTP transaction are on different transaction managers.** `InitOrderService.createOrder` is `@Transactional` with no qualifier, and `mainTransactionManager` is `@Primary` (MainDataSourceConfiguration:61), while `B2BOrderAuditRepo` lives under `repository.custom` and is bound to `customTransactionManager` (CustomDataSourceConfiguration:23-26). The audit write is therefore not enrolled in the method's transaction. Whether a *rejected* order's audit row is present is a question for the table, not for reasoning about rollback.
- **Working-tree divergence at the time of writing.** Three tracked files are modified but uncommitted, and two of the changes matter: `B2BShipToPartnerFunctionStrategy:120` has `throw new OrderValidationException("Ship to address is mandatory")` **commented out** (so ship-to rejection would create a case and let the order continue with a null ship-to), and `SalesforceContactService` has its entire search body commented out. This document describes commit `22bae2d`, where both are live. If behaviour on a host disagrees, check what was actually deployed.
- **Seven untracked `~` backup files** exist in `src/`, including `SalesforceSalesAssistantPartnerFunctionStrategy.java~`, for which no tracked `.java` counterpart exists. None contains a logging statement, so they cannot have contributed message text to the counts in §7.1 — but do not treat their contents as live code.
- **`debug=true` is set in every non-prod profile** and absent from prod. Expect Spring's auto-configuration report noise in dev/sit/test/cte/ua2/preprod.
- **Benign startup/shutdown lines:** `The application is initializing, starting eligible RabbitMQ listeners.` and `{} listeners have been set to status STOPPED` are normal lifecycle events. `Invalid queue type specified […]` at startup is *not* benign — the application will not start.

## 12. Escalation

| Symptom | Route | Business or fault? |
|---|---|---|
| Validation rejection (§4.1–4.4), ship-to rejection (§4.6) | the ordering/customer-data team via the Salesforce case already raised | **BUSINESS — do not raise a ticket** |
| `OrderValidationException occurred for eventId = …` WARN | same — a case exists, a human must act | **BUSINESS** |
| Order created but not visible in order search | check the auto-approval branch in §3.1 before escalating; may be correct behaviour | **BUSINESS** |
| Messages piling up on a queue, nothing logged | listener stopped — CSD Admin Tool / `GET /admin/status`, then `LISTENER_STATUS.autoStart` | fault or deliberate |
| Anything on a `*_error` queue | inbound-order owners. **Nothing replays these automatically** | FAULT |
| `Invalid order origin`, `Could not find conversion rate from … to …`, `Unsupported date format` | inbound-order owners | FAULT (usually bad inbound data) |
| `Error while calling settings…` / `Error while calling JAM…` | Settings WS / JAM owners | FAULT, downstream |
| `Create Salesforce case request failed: {}` | Salesforce / BTP integration owners; the message is on `salesforce_create_case_error` and can be replayed to `salesforce.create.case` | FAULT, downstream |
| `Failed to send AMQP failure notification email…` | mail infrastructure — **the underlying order failure is separate and still needs handling** | FAULT, cosmetic |
| 400 with no log line | inbound-order owners, but ask the caller for their exact request body first — the service recorded nothing | FAULT in the caller's payload |

**Hand over:** the `eventId` (or the `messageReference` plus a note that it must be translated via `b2b_order_audit.boa_inb_msg_ref`); the `correlationId` if the line came from `CustomRabbitListenerErrorHandler`; the error queue name; a timestamp window **with the host's timezone stated explicitly**, because the log records none; and the environment/profile.
