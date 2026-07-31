```yaml
service: "order-validator-web-service"
aliases: ["Order Validator Web Service", "order validator"]
repo: "order-validator-web-service"
log_files: ["order-validator-web-service.log"]
log_format: "plain text"
host_retention: "SizeAndTimeBasedRollingPolicy inherited from Spring Boot's bundled file-appender.xml (not overridden in this repo); no rolling property is set in any application*.properties"
environments: ["local", "dev", "sit", "test", "cte", "preprod", "prod"]
correlation_id: "none — no MDC.put, no correlationId, no tracing dependency anywhere in the repo"
primary_join_key: "none appears in log text; the only business value ever logged is accountNumber, from one INFO line"
statuses_written: []   # this service writes no status anywhere; it has no database, no queue and no outbound client
```

# order-validator-web-service

## 1. What this service does

This service answers one question: **"given this order as it stands right now, what is wrong with it?"** A caller sends the whole order — header, order lines, partner functions (Sold-to / Ship-to / Bill-to / Payer / End customer addresses) and text lines — plus the name of a *strategy*: a named, ordered list of checks matching a moment in the user's journey (pressing Save on a new order, Save-and-Submit, Validate while viewing an order, or an order arriving from an inbound channel). It runs that list and returns a list of messages. Each message says which tab to display it on, which field it belongs to, which line numbers are affected, and whether it is an `ERROR` or `INFORMATION` (`ValidationMessageDto.java:17-21`, `ValidationType.java:4`, `TabType.java:4-13`).

The checks are the business rules of order entry: customer order reference filled in, currency present, delivery date not in the past, prices and quantities in range and parseable, addresses complete and within SAP field lengths, PO line numbers not duplicated, bundle (BOM) components consistent, print-switchboard settings coherent, contract call-off lines in stock. Message text comes from a translation bundle in English, French and German (`i18n/messages_en.properties`, `messages_fr_FR.properties`, `messages_de_DE.properties`), chosen from the `locale` field inside the order (`ValidationStrategy.java:23`).

## 1.1 What order-validator-web-service is *not* responsible for

**Responsibility ends the moment the HTTP response is written.** It returns a JSON array of messages and forgets the request. There is no database (`OrderValidatorApplication.java:11` excludes `DataSourceAutoConfiguration` and `HibernateJpaAutoConfiguration`), no queue publisher or listener, and no outbound HTTP/Feign/RestTemplate/WebClient client anywhere in `src/main/java`. It therefore does **not**:

- **Save, submit, approve, reject or lock anything.** It does not write any order status. `ValidateOrderForSubmission.java:38-43` *reads* an incoming `orderStatus` string and complains if it is already `Submitted`, `Contract locked, attempting retry` or `Order Rejected` — reading, not writing.
- **Decide what to do with the messages.** Whether an `ERROR` blocks a save is the caller's decision; this service has no concept of blocking. Note especially that `Validator.failFast()` defaults to `false` (`Validator.java:11-13`) and **no validator in the repo overrides it**, so the early-exit branch at `ValidationStrategy.java:28-30` is never taken: every strategy always runs every one of its validators.
- **Fetch reference data.** Allowed warehouses, allowed plant codes, valid internal contracts, software-licence product codes, linked partner functions and every feature toggle used by the rules arrive *inside the request* on `SalesOrderAttributes` (`SalesOrderAttributes.java:21-37`). If a rule fires because a list was empty, the list came from the caller.
- **Own the strategy-to-button mapping.** The strategy names describe caller intent, but nothing in this repo decides which strategy a given screen or channel calls.
- **Authenticate users.** It uses a single HTTP Basic service account per environment (`application.properties:11-12`, `application-prod.properties:4-5`), not end-user identity. There is no user id, no rights model and no audit column anywhere in the codebase.

## 2. Key concepts

| Term | What it actually means in this code |
|---|---|
| **Strategy** | A named, ordered list of validator class names, loaded from a JSON file at startup (`StrategyConfig.java:24-37`, files in `src/main/resources/strategy/`). Nine strategies exist. |
| **Validator** | A Spring bean implementing `validate(SalesOrder) → List<ValidationMessageDto>` (`Validator.java:8-14`). 81 `@Component` validator beans exist under `validators/`; 76 distinct names are referenced by the nine strategy files. |
| **Validation message** | `type` (`ERROR` or `INFORMATION`), `attributeName` (the field path), `message` (already-translated text), `tab`, `lineNumbers` (`ValidationMessageDto.java:17-21`). The 4-, 3- and 2-arg constructors default `type` to `ERROR` (`ValidationMessageDto.java:23-33`). |
| **Tab** | Where the caller should display the message: `HEADER`, `HEADER_UDF`, `BASIC_PRICING`, `REBATES`, `COSTS_SOURCING`, `DETAILED_PRICING`, `TEXT_OTHER`, `BLOCKING_GROUPING`, `FEES`, `GLOBAL_ERRORS` (`TabType.java:4-13`). |
| **Line sequence** | The identifier put into `lineNumbers` and into `{0}` placeholders — `OrderLine::getLineSequence` (e.g. `ValidateInternalContract.java:46`). Distinct from `getLineNumber()`, which the print-switchboard rules use for consolidation targets (`ValidateOneTouchPrintingSwitchboard.java:184`). |
| **Material status** | Read from `OrderLine.getEolText()` (`ValidateLineProductsForOrder.java:105`). `null`, `10`, `11`, `12`, `20`, `25` and **`40`** are all accepted by the add-to-order check (`:101-107`). Status `40` additionally requires the line to be a call-off line, i.e. to carry both a contract number and a contract line number (`:83-85`, `:122-125`). |
| **Call-off line** | A line whose `selectedSource` has both a contract number **and** a contract line number (`ValidateLineProductsForOrder.java:122-125`, `ValidateCallOffQuantities.java:59-62`). |
| **ZMAT BOM / generic enterprise BOM** | A bundle: a ZMAT header line with component lines. Components must share a supplier and a source, and must not be printed inconsistently (`ValidateGenericEnterpriseBom*`, `ValidateComponentsInsideBom`). |
| **Print switchboard** | Per-line "print with price / print without price / do not print / consolidate into line N" settings. Gated entirely by `salesOrderAttributes.printSwitchboardValidationEnabled` (`ValidateOneTouchPrintingSwitchboard.java:100`). |
| **Partner function role** | SAP partner codes: `SOLD_TO`=AG, `SHIP_TO`=WE, `BILL_TO`=RE, `PAYER`=RG, `END_CUSTOMER`=ZE, `ACCOUNT_MANAGER`=V1, `SALES_ASSISTANT`=Z1, `SECOND_PAYER`=RG/RG2, `SECOND_PAYER_AND_BILL_TO`=RG/RG3, `COURIER`=CR (`PartnerFunctionRole.java:9-18`). |
| **`services-enabled`** | The only custom application property, `order.validator.config.services-enabled=true` (`application.properties:37`, `CustomConfig.java:10-14`). It is read in exactly two places: `ValidateServicesForOrder.java:114` and `ValidateBracketing.java:127`. |

### 2.1 Concepts this document does not cover

Decline rather than infer on all of the following — none of them has a verifiable meaning in this repo:

- **What a "strategy" name maps to in the UI.** The names imply buttons, but no code here binds a strategy to a screen.
- **Order status lifecycle.** `OrderStatusEnum.java:6-9` lists nine strings; only three are compared against (`ValidateOrderForSubmission.java:38-42`). What the others mean, and who sets them, is not in this repo.
- **`ValidateShipToWithAvalara`.** Despite the name, it contains no tax logic, no Avalara call and no address check — it logs and returns empty (`ValidateShipToWithAvalara.java:17-20`). Same for the other 19 stubs listed in §3.1.
- **Any external system.** Track & Trace, SPT, SAP, Avalara: mentioned only in comments and a `tandtGuid` field (`CartHeaderAttributes.java:133-134`). No integration code exists.
- **`SalesOrderAttributes` toggle provenance.** Who sets `printSwitchboardValidationEnabled`, `salesOpportunityEnabled`, `isShipFixedDateDelivery`, etc. is outside this repo. Note the misleading Javadoc at `ValidateOpportunityID.java:19-22`, which claims a "custom config SALES_OPPORTUNITY_ENABLED"; the code actually reads the per-request field `salesOrderAttributes.isSalesOpportunityEnabled()` (`:36`).
- **The log line pattern and rolling policy** — see §7, they are inherited from a dependency, not defined here.
- **Host paths, deployment topology, and which environments actually run.** `application.properties:18-19` points at `d:/apps/order-validator-web-service`, implying Windows hosts, but nothing here confirms deployment.
- **HTTP status codes for malformed requests.** `@Valid`/`@NotEmpty` failures (`ValidationStrategyController.java:30-31`) are handled by `StandardizedControllerExceptionHandler`, a superclass from the `cc-standardized-web-app` dependency (`ControllerExceptionHandler.java:19`), whose behaviour is not in this repo.

## 3. How work flows through this service

**Startup** — `StrategyConfig.strategyMap` globs `classpath*:strategy/*.json`, throws `StrategyParserException("no strategy JSON file found!")` if none match, then for each file logs at INFO and deserialises it (`StrategyConfig.java:24-37`). `StrategyDeserializer` maps each validator name string to a bean by `getClass().getSimpleName()` and throws `StrategyParserException` on an empty name or an unknown name (`StrategyDeserializer.java:26-46`). **A typo in a strategy file therefore fails application startup, not a request.**

**Per request** — `POST /api/strategy/{strategyName}/run` (`ValidationStrategyController.java:29-33`) → `ValidationStrategyService.runStrategy` looks the strategy up and throws `NoSuchElementException("no validation strategy was found")` if absent (`ValidationStrategyService.java:18`, `:45-50`) → `ValidationStrategy.run` sets the thread locale from `order.getLocale()` and loops the validators, appending each one's messages (`ValidationStrategy.java:21-34`) → the accumulated list is returned as `200 OK` with a JSON array.

The nine strategies, by the `name` inside each file:

| Strategy name | File | Validators |
|---|---|---|
| `create-order-press-save` | `create-order-press-save.json` | 5 |
| `edit-order-press-save` | `edit-order-press-save.json` | 5 |
| `inbound-order-validate` | `inbound-order-validate.json` | 41 |
| `view-order-press-submit` | `view-order-press-submit.json` | 55 |
| `view-order-press-validate` | `view-order-press-validate.json` | 57 |
| `create-order-press-save-submit` | `create-order-press-save-submit.json` | 59 |
| `create-edit-order-press-validate` | `create-edit-order-press-validate.json` | 60 |
| `edit-order-press-save-submit` | `edit-order-press-save-submit.json` | 60 |
| `inbound-order-auto-approval-validate` | `inbound-order-**autoapproval**-validate.json` | 71 |

**The last row is the one to remember: the file is `inbound-order-autoapproval-validate.json` but the strategy name callers must send is `inbound-order-auto-approval-validate`** (`inbound-order-autoapproval-validate.json:2`). The lookup key is the `name` field, not the filename (`StrategyConfig.java:35`).

Two supporting endpoints exist: `GET /api/strategy` lists strategy names and `GET /api/strategy/{strategyName}` lists that strategy's validator class names in order (`ValidationStrategyController.java:43-51`, `ValidationStrategyService.java:25-43`). The listing order of `GET /api/strategy` is a `HashMap` key set (`StrategyConfig.java:31`) and so is arbitrary; `GET /api/strategy/{name}` *is* in execution order.

### 3.1 Why a step gets skipped

- **The strategy does not include it.** The five-validator save strategies check only customer order reference, currency, submitted/rejected status, sales-price range and internal contract. Nothing else is checked on a plain Save — so "I saved and it did not complain about X" is usually just strategy scope.
- **The validator is a stub that logs and returns nothing.** Exactly twenty validators consist only of `log.warn("Not implemented");  return Collections.emptyList();`: `ContactCenterValidateExtendedPrice`, `ContactCenterValidatePriceOverrides`, `ValidateAutoApprovalCarePackLinks`, `ValidateAutoApprovalDelivery`, `ValidateAutoApprovalMaterialStatus`, `ValidateAutoApprovalMixedSource`, `ValidateAutoApprovalOrderLine`, `ValidateAutoApprovalOrderLinesShipTo`, `ValidateAutoApprovalPreferredSuppliers`, `ValidateAutoApprovalProductManuAndClassifications`, `ValidateAutoApprovalRequiredDate`, `ValidateAutoApprovalShipComplete`, `ValidateAutoApprovalShipToOverrideB2B`, `ValidateAutoApprovalSource`, `ValidateGenericEnterpriseBomRebates`, `ValidateOrderLockStatus`, `ValidateOrderTextTotalLines`, `ValidateSalesAssistantPartnerFunction`, `ValidateSalesPrice`, `ValidateShipToWithAvalara`. **Fifteen of the twenty are in `inbound-order-auto-approval-validate`**, including twelve of the thirteen entries in that strategy's auto-approval-specific block — so the auto-approval block is almost entirely inert and that strategy currently performs little more than the same general order checks as the others. The one real member of that block is `ValidateAutoApprovalBillShipPayerB2B`, which requires a `PAYER`, `SECOND_PAYER` or `SECOND_PAYER_AND_BILL_TO` partner function and otherwise returns `Please assign a {0} partner function.` with the literal label `Payer` (`ValidateAutoApprovalBillShipPayerB2B.java:28`, `:41-46`). Stub counts for the other strategies: 3 each for `create-edit-order-press-validate`, `create-order-press-save-submit`, `edit-order-press-save-submit`, `view-order-press-submit` and `view-order-press-validate`; 2 for `inbound-order-validate`; 0 for both five-validator save strategies.
- **A per-request toggle switched it off.** `printSwitchboardValidationEnabled` false skips all print-switchboard rules and logs an INFO (`ValidateOneTouchPrintingSwitchboard.java:100-105`); `salesOpportunityEnabled` false skips the Opportunity ID requirement (`ValidateOpportunityID.java:36`); `isShipFixedDateDelivery` false skips the optional delivery date/time rules (`ValidateOrderHeaderDateFields.java:75`); `allowDuplicatePOs` true skips the duplicate PO-line check and logs a DEBUG (`ValidateCustomerLineNumbers.java:40-43`).
- **A country gate.** The asseting/config service rules only run when the header country code is `GB` (`ValidateServicesForOrder.java:65`, `:113`); the CPQ warehouse-allowed check only runs when `orderOrigin` is CPQ's `originName` (`ValidateOrderFields.java:67-69`); the single-DD-supplier information message is suppressed for B2B/SnD/SNOW origins (`ValidateOneTouchPrintingSwitchboard.java:394-396`, `OrderOriginEnum.java:31-33`).
- **The value was null.** Many rules skip nulls by design, e.g. an internal contract reference is only checked when non-null (`ValidateInternalContract.java:60`), and the order/delivery date rules only fire when the date is present (`ValidateOrderHeaderDateFields.java:60`, `:67`).

## 4. The rules that stop something

All message text below is verbatim from `src/main/resources/i18n/messages_en.properties`, with `{}` placeholders as written. Every one of these is a **BUSINESS** outcome — the order data needs correcting by a person. None of them is a fault in this service.

**4.1 Order already in a terminal state** — `ValidateOrderForSubmission.java:38-43`. Incoming `orderStatus` equal to `Submitted` **or** `Contract locked, attempting retry` → `Order is already submitted.`; equal to `Order Rejected` → `This order has been rejected.` Field `orderStatus`, tab `HEADER`.

**4.2 Required header fields** — `ValidateRequiredQuotesAndOrdersHeaderFields.java:51-55` composes `CC_REQUIRED_FIELD` (`{0} - You must fill in this field.`) with `Customer order ref`. `ValidateOrderFields.java:83-86` uses the standalone forms instead: `Customer order ref - You must fill in this field.`, `Warehouse - Please select a warehouse.`, `Date customer order - You must fill in this field.`, `Date delivery required - You must fill in this field.` `ValidateOrderSalesOffice.java:32-34`: `Sales Office - You must fill in this field.` `ValidateQuotesAndOrdersCurrency.java:40-42`: `The currency code is invalid.` (emitted when the currency is *empty*, not when it is malformed). `ValidateOpportunityID.java:39-41`: `Opportunity ID - You must fill in this field.`

**4.3 Dates** — `ValidateOrderHeaderDateFields.java:60-101`. Order date after today → `Date customer order - Please enter date less than or equal to today's date.` Delivery date before today → `Date delivery required - Please enter date greater than or equal to today's date.` Optional delivery date/time incomplete or in the past → `Delivery date and time - Please populate with both fields, ensuring the date is greater or equal to today's date.` The `field - message` join is `FIELD_ERROR_MESSAGE_PATTERN` (`Constants.java:13`). Wrong format on order/delivery date, from `ValidateOrderFields.java:89-93`: `CC_ERR_UDF_DATE_FORMAT` = `{0} - Must be in the format {1}.` with the field name capitalised and the format `yyyy-MM-dd` (`DateUtils.java:12`).

**4.4 Price and quantity** — `ValidateSalesPriceAndQuantity` / `ValidateSalesPriceRange`, bounds in `PricingUtil.java:18-22`. Quantity below 1 → `Quantity - Must be greater than or equal to 1.` Quantity above 99999999 → `The maximum quantity that can be ordered for a single line is {0}`. Unparseable or negative price → `Unit Sell Price - Value not in the expected format.` Unit price above 999,999,999.9 → `Unit Sell Price - Maximum value exceeded.` Line value (price × qty) above 99,999,999,999.9999 → `Line value - Maximum allowable value exceeded.` Order total above 9,999,999,999,999.99 → `The maximum total sales price of {0} has been exceeded` — but only when nothing else failed (`ValidateSalesPriceAndQuantity.java:74-76`). Text-item lines are excluded from all of this (`ValidateMaximumSalesPrice.java:74-76`).

**4.5 Addresses and partner functions** — `ValidatePartnerFunction.java`. Checked for `BILL_TO`, `PAYER`, `SHIP_TO`, `END_CUSTOMER` (`:62`); the field-level checks below run only for `SHIP_TO` and `END_CUSTOMER` (`:63`, `:81-83`). Missing account number → `Please assign a {0} partner function.` and **nothing further is checked for that role**. Then: missing name/street/town → the label plus ` - ` plus `Please enter a value for the {0} partner function.`; bad region → `Region - {0} address: Region value is invalid.`; bad post code → `Post Code - This field is not in the expected format for the {0} partner function.`; bad country → `{0} / Country code - Please select valid value.`; and length caps `Number/Street - {0}: Value cannot be greater than 40 characters in length`, `District - {0}: …40…`, `CO - {0}: …35…`, `Building - {0}: …35…`, `Name - {0}: …35…`, `Name 2 - {0}: …35…`.

**4.6 Products** — `ValidateLineProductsForOrder.java:59-65`. Material status outside the accepted set, **or** `additionalAttributes.skuStatusCode` not equal to `A` (`StatusCode.java:8`, `:88-92`) → `The order contains EOL materials.` A status-`40` line that is not a call-off line → `EOL 40 material on {0} must be sourced from contract.` **Both messages are emitted twice** — once on `BASIC_PRICING` and once on `GLOBAL_ERRORS` (`:127-135`). `ValidateOrderNonCatOrderLines.java:42-47` likewise emits `This order contains non-catalogue items in the following line(s): {0}` twice.

**4.7 Contract call-off stock** — `ValidateCallOffQuantities.java:45-57`. A line sourced from a contract whose contract/line no longer appears in the line's contract list, or whose latest-starting matching contract has less available quantity than ordered → `There is insufficient stock on the selected contract order used on order line(s): {0}` on tab `COSTS_SOURCING`.

**4.8 PO line numbers** — `ValidateCustomerLineNumbers.java:45-51`. Duplicate non-blank customer line numbers among non-BOM-component lines → `PO line number duplicate`, once per duplicate line, each carrying the full duplicate list.

**4.9 At least one order line** — `ValidateMinimumNumberOfOrderLines.java:27-33`. No non-delivery-item line → `Order must contain at least one order line.`

**4.10 Internal contract** — `ValidateInternalContract.java:49-54`. A non-null `internalContractReference` not present in the request's `internalContracts` set → `The internal contract value is invalid at the following lines(s): {0}.` (note the typo `lines(s)`, which is in the bundle).

**4.11 Optional items** — `ValidateForOptionalItems.java:41-44`. Singular vs plural differ: one line → `Optional Item present at line {0}.`; more than one → `Optional Items present at lines {0}.`

**4.12 Print switchboard** — `ValidateOneTouchPrintingSwitchboard.java`. Eleven distinct `ERROR` messages plus a banner. Examples, verbatim: `At least one line must be printed with a price.`, `Line {0} cannot be set to print with price and consolidate into another line.`, `Invalid Print Switchboard setting for line {0}`, `Line {0} consolidated into a line that does not exist.`, `Vendors on DD lines with Print Switchboard settings do not match`. The banner, prepended at index 0 *and* appended for `GLOBAL_ERRORS` (`:469-482`), is `The printing switchboard settings require your attention on the following line(s): {0}` — its `{0}` is the union of line numbers from the other messages in the same run, so **it is derived, not an independent finding**.

## 5. What users can do

Callers can do exactly three things (`ValidationStrategyController.java`): run a strategy against an order, list strategy names, list one strategy's validators. All three are under `/api/**` and require HTTP Basic authentication (`WebSecurityConfig.java:33`, `:52`); `/health/**` is open and `/info` and `/health_details` require authentication (`:49-50`). Sessions are stateless and CSRF is disabled (`:44`, `:53`).

"I called it and it did not do what I expected" resolves, in priority order, to:

1. **Wrong strategy name → HTTP 400**, not an empty result. `NoSuchElementException` carrying `no validation strategy was found` is mapped to `BAD_REQUEST` (`ValidationStrategyService.java:47`, `ControllerExceptionHandler.java:21-24`). Most likely cause: sending `inbound-order-autoapproval-validate` (the filename) instead of `inbound-order-auto-approval-validate` (the name).
2. **The strategy does not contain that check** — see §3.1, and use `GET /api/strategy/{strategyName}` to see the actual list.
3. **The check is one of the 20 stubs** — it will always return nothing. This is silent: nothing distinguishes "checked and clean" from "not implemented" in the response.
4. **A per-request toggle turned it off** — §3.1. Only two of these produce any log evidence (print switchboard at INFO, duplicate POs at DEBUG); the rest are entirely silent.
5. **401** if Basic credentials are wrong; the entry point sends `WWW-Authenticate: Basic` and `Not authorized` (`WebSecurityConfig.java:66-67`).

There is no rights model, no locking, no concurrency control and no per-user restriction in this service at all.

## 6. Statuses, and who writes them

| Status | Written by |
|---|---|
| — | — |

**This service writes exactly 0 statuses.** There is no persistence layer to write to: `OrderValidatorApplication.java:11` excludes datasource and JPA auto-configuration, and no repository, entity, queue or outbound client exists in `src/main/java`. `OrderStatusEnum.java:6-9` is a read-only comparison table; the only statuses ever compared are `Submitted`, `Contract locked, attempting retry` and `Order Rejected` (`ValidateOrderForSubmission.java:38-42`). Treat any question of the form "who set this order to X?" as belonging to a different service.

---

## 7. Log anatomy — read this before quoting any line

Config: `src/main/resources/logback-spring.xml`. It sets `LOG_FILE` to `${LOG_FILE:-${LOG_PATH:-${LOG_TEMP:-${java.io.tmpdir:-/tmp}}}/order-validator-web-service.log}` (`:4-5`), then includes Spring Boot's bundled `defaults.xml`, `console-appender.xml` and `file-appender.xml` (`:6-8`), and attaches only `FILE` to the root logger at `INFO` (`:9-11`).

**The line pattern is not defined in this repository.** It is `FILE_LOG_PATTERN` from the Spring Boot jar's `org/springframework/boot/logging/logback/defaults.xml`, which this repo merely includes at `logback-spring.xml:6`. Parsing hazards that follow from that pattern:

- Fields are: ISO-8601 timestamp **with offset** (`yyyy-MM-dd'T'HH:mm:ss.SSSXXX` — a timezone offset *is* recorded), level right-padded to width 5, PID, a literal ` --- `, the thread name in brackets, then the logger name **truncated and abbreviated to 40 characters with package shortening**, then ` : `, then the message.
- **Logger names are abbreviated.** `com.computacenter.ordervalidator.validators.ValidateOneTouchPrintingSwitchboard` will not appear in full — expect a shortened form such as `c.c.o.v.ValidateOneTouchPrintingSwitchboard`. Match on the class simple name, never on the full package.
- Stack traces are appended to the same event by the exception converter and span multiple physical lines; a new event starts at the next line beginning with a timestamp.
- The correlation-id slot in the pattern exists but is **always empty here** — see §8.

The only appender attached in any profile is `FILE`, except under the `local` profile where `CONSOLE` is added as well (`logback-spring.xml:12-21`).

### 7.1 Level availability per environment

Root is `INFO` (`logback-spring.xml:9`). Per profile, for `com.computacenter.ordervalidator`:

| Profile | `com.computacenter.ordervalidator` | `org.springframework*` |
|---|---|---|
| `local` | DEBUG (+ console) | `org.springframework` INFO |
| `dev`, `sit`, `test`, `preprod`, `cte` | DEBUG | `org.springframework` INFO |
| `prod` | **INFO** | `org.springframework.**web**` ERROR — **only that sub-package**; the rest of Spring falls through to root INFO |

Counted off `git ls-files '*.java'` (identical for `src/main` alone — there are no log statements in tests), the entire tracked codebase contains **26 log statements**:

| Level | Count |
|---|---|
| TRACE | 0 |
| DEBUG | 1 |
| INFO | 4 |
| WARN | 20 |
| ERROR | 1 |

The visibility story is the opposite of the usual one: **prod loses only a single DEBUG statement.** Twenty-five of the 26 statements are visible in production. But the surface is so thin that there is almost nothing to see — 20 of the 26 are the same `Not implemented` WARN, and there is **no request-received, no request-completed, no validation-result and no per-validator log line anywhere**. A request that returns 40 errors and a request that returns none produce identical log output.

### 7.2 Retries

**There are none, and there is nothing to retry.** No `@Retryable`, no resilience4j, no circuit breaker, no `RestTemplate`/`WebClient`/Feign client and no queue listener exists anywhere in `src/main/java` or `src/main/resources`. No timeout property is configured in any `application*.properties`.

Consequently: **every log line in this service is one logical event; never collapse duplicates as retries.** Repetition means genuine repetition — most often the `Not implemented` WARN, which is emitted once per stub validator per request. A single `inbound-order-auto-approval-validate` call produces **15 identical `Not implemented` WARNs, 11 of them consecutive**, distinguishable only by their logger class. Every other strategy produces 3, 2 or 0 of them, never two in a row.

### 7.3 Retention and file names

The file name on disk is set by a property, not by the logback default, and the two disagree in a way that matters:

- `logback-spring.xml:5` would place the file under the temp dir, but `application.properties:18` overrides it: `logging.file.name = d:/apps/order-validator-web-service/order-validator-web-service.log`.
- Under the `local` profile that becomes `c:/temp/logs/order-validator-web-service/order-validator-web-service.log` (`application-local.properties:4`).
- Tomcat access logs go to a separate directory, `d:/apps/order-validator-web-service` (`application.properties:19`) or `c:/temp/logs/order-validator-web-service` locally (`application-local.properties:5`). **The access log is a second log stream and is not the file above.**

Rolling is whatever Spring Boot's included `file-appender.xml` provides — `SizeAndTimeBasedRollingPolicy`, driven by `LOGBACK_ROLLINGPOLICY_*` properties. **No `logging.logback.rollingpolicy.*` property is set in any `application*.properties` in this repo**, so retention is at the dependency's defaults or set by the host. Confirm on the host before quoting a retention figure.

## 8. Identifiers in the logs

**No correlation id exists.** `grep` for `MDC`, `correlationId`, `traceId` and any tracing dependency returns nothing across `src/main/java`, `src/main/resources` and `pom.xml`. Nothing is put into the MDC on any code path — not the HTTP path, not anywhere, because there is only one path.

Only **one** business value is ever written into log text, in a single line (`ValidateOneTouchPrintingSwitchboard.java:104`):

> `Print switchboard validation disabled for this account: {}`

where `{}` is `order.getAccountNumber()`. That is a customer account number, **local to this line**, and it appears only when print-switchboard validation is *off*.

Everything else that would let you join to another system is absent from the logs even though it is present in the request: `header.headerId`, `header.cartHeaderAttributes.headerNumber`, `tandtGuid`, `orderCustomerReference` and `userid` (`OrderHeader.java:19`, `CartHeaderAttributes.java:133-142`) are **never logged**. To locate a specific order's validation run in the log file you have only the timestamp and the thread name.

## 9. Journey stages and their log evidence

There is exactly one stage with log evidence — **startup** — plus three incidental in-flight lines. Every message below is verbatim, with its level and its emitting class.

**Startup: strategies loaded** — INFO, `com.computacenter.ordervalidator.config.StrategyConfig` (`StrategyConfig.java:33`):
> `Loading strategy from {}`

`{}` is `resource.getFilename()`, i.e. the **file name** (`inbound-order-autoapproval-validate.json`), not the strategy name. Expect nine of these at startup. **Fewer than nine means a strategy file is missing from the artifact.**

**Startup: strategy parse failure** — no log line at all. `StrategyParserException` is thrown, not logged, from three places with these exact messages: `no strategy JSON file found!` (`StrategyConfig.java:27`), `error in deserializing the validators from the strategy file` (`StrategyDeserializer.java:20`), `validator name is empty: ` (`:21`, note the trailing space and colon), and `cannot find bean with validator name ` (`:22`, trailing space) with the offending name appended. These surface as bean-creation failures in the Spring startup stack trace.

**In-flight: stub validator reached** — WARN, emitted by 20 different classes, all with identical text (`ValidateOrderLockStatus.java:18` and 19 siblings):
> `Not implemented`

The **emitting logger class is the only way to tell which validator ran**. The text is uniform across all 20 — there is no variant wording in this family.

**In-flight: print switchboard skipped** — INFO, `ValidateOneTouchPrintingSwitchboard` (`:104`):
> `Print switchboard validation disabled for this account: {}`

**In-flight: duplicate PO check skipped** — DEBUG, `ValidateCustomerLineNumbers` (`:41`):
> `Allowed Duplicates on CustomerLines`

Not visible in prod (§7.1). Note this line is emitted for **two different reasons**: an empty order-line list *or* `allowDuplicatePOs` true (`:40`). The text does not distinguish them.

**In-flight: validation started / finished / found N problems** — **no such line exists.** Do not look for one.

**Startup/runtime: hostname lookup failed** — ERROR, `HostnameInfoContributor` (`:26`):
> `Hostname is unknown`

Logged with the `UnknownHostException` attached; the actuator `/info` then reports `hostname: unknown` (`:27`). This is cosmetic and affects nothing else.

Two further INFO lines exist but carry no operational information: `Validating internal contracts for order lines.` (`ValidateInternalContract.java:40`) and `Validating line UDF fields` (`ValidateOrderLineUdfFields.java:34`). Both are unconditional entry markers with no result, no ids and no counts — their presence tells you only that the strategy included that validator.

### 9.1 Lines that look like errors but are not

- **`Not implemented` (WARN)** is the normal, expected state of this service on every request that touches one of the 20 stub validators. It is not an incident and needs no ticket. Eleven in a row, and fifteen in total, is normal for one `inbound-order-auto-approval-validate` call.
- **`Hostname is unknown` (ERROR)** is the only ERROR statement in the codebase and is a benign actuator/info-page degradation.
- **The single-DD-supplier message is not an error even though it is worded like one.** `Usage of Print Switchboard with DD lines should only be used in exceptional circumstances, you must ensure that the purchasing department raise a single purchase order for the relevant DD lines and that these lines are booked in together to ensure the customer invoice creates correctly` is emitted with `type = INFORMATION` (`ValidateOneTouchPrintingSwitchboard.java:397-398`) — **the only `INFORMATION` message in the whole service** (`grep INFORMATION`, two hits: the enum and this line). Its bundle key is `CC_WARNING_DIRECT_DELIVERY_SINGLE_SUPPLIER`; do not let the `WARNING_` prefix or the `CC_ERR_`/`CC_INVALID_` prefixes elsewhere decide severity — read `type`.
- **Informational-sounding response messages that *are* `ERROR`s**: `This order contains non-catalogue items in the following line(s): {0}`, `Optional Item present at line {0}.`, and the print-switchboard banner all default to `ERROR` via the short constructors (`ValidationMessageDto.java:23-33`).

### 9.2 Queue / topology reference

**None.** This service publishes to no queue, consumes from no queue, and has no exchange, routing key, dead-letter exchange or error queue. There is no AMQP/JMS dependency or configuration anywhere in the repo. Everything it does happens inside one synchronous HTTP request.

## 10. Evidence that is not in the log file

- **The HTTP response body is the primary evidence** and it is nowhere in the log file. If you need to know what a run returned, the caller must have captured it; this service keeps no copy. **This is a current-state answer computed on the fly, not history** — re-running the same strategy against a since-edited order gives different results, and there is no way to reconstruct what it said earlier.
- **The Tomcat access log** (`cc.tomcat.accesslogvalve.directory`, `application.properties:19`) is a separate file and the only place a request is recorded at all — URL, status, timing. It is the only way to correlate a validation call with a clock time. Its format and fields come from the `cc-standardized-web-app` dependency and are not defined in this repo.
- **No tables, no queues, no headers.** There is no database, so there are no audit columns; `created_by`/`modified_by` do not exist here in any form. **Refuse "who did this?" questions outright** — this service records no actor. `CartHeaderAttributes.userid` (`:142`) arrives in the request and is never logged or stored.
- **Actuator endpoints** expose current state, not history: `/health_details` and `/info`, both authenticated, base path `/` (`application.properties:25-31`). `/info` includes the hostname (`HostnameInfoContributor.java:23-24`) and env properties (`management.info.env.enabled=true`, `application.properties:30`). There is also an unauthenticated `GET /health` returning the fixed literal `{"status":"UP"}` — a hard-coded constant, **not a real health check** (`PublicHealthController.java:11-14`, permitted at `WebSecurityConfig.java:49`). Never treat a `/health` 200 as evidence the service is functioning.
- **Secrets:** `application.properties:12` and `:46` and `application-prod.properties:5` hold jasypt-`ENC(...)` values for the API password and the keystore password, decrypted with `encrypt.key` (`application.properties:2`). A PKCS12 keystore is committed at `src/main/resources/static/ssl/ordervalidatorws.computacenter.com.pfx` (`application.properties:45`). Redact all of these when quoting config.

## 11. Blind spots and traps

- **Locale is set on the thread and never cleared.** `ValidationStrategy.java:23` calls `LocaleContextHolder.setLocale(order.getLocale())` with no `finally` and no reset. On a pooled Tomcat thread the locale from one request persists into the next request that does not set it. `SalesOrder.locale` has no `@NotNull` (`SalesOrder.java:16`), so a caller omitting it leaves whatever the previous request left behind. **Treat "the message came back in the wrong language" as plausible and reproducible under load, not as user error.**
- **Two validators freeze their locale at startup.** `ValidateMaximumSalesPrice`'s constructor reads `LocaleContextHolder.getLocale()` and caches `locale`, `numberFormat` and the translated `Not priced` string into final fields (`ValidateMaximumSalesPrice.java:35-41`). These are singletons, so `ValidateSalesPriceAndQuantity` and `ValidateSalesPriceRange` do their number parsing and their "is this line priced?" comparison against the **JVM-startup locale for the lifetime of the process**, regardless of the request's locale. Price-format complaints from a non-English caller are a known hazard here.
- **A message with `message = null` is emitted deliberately.** `ValidateOrderHeaderDateFields.java:82` adds `new ValidationMessageDto(OPTIONAL_DELIVERY_TIME, null, HEADER)` as a field-marker companion to the real message. A null `message` in the response is expected output, not corruption.
- **Duplicated messages are by design, not double-counting.** `ValidateLineProductsForOrder.java:127-135` and `ValidateOrderNonCatOrderLines.java:43-46` each return the same text twice, differing only in `tab` (`BASIC_PRICING` vs `GLOBAL_ERRORS`); the print-switchboard banner is likewise added twice (`ValidateOneTouchPrintingSwitchboard.java:479-480`). **Never quote a message count as a problem count.**
- **`failFast()` is dead code.** No validator overrides it (`Validator.java:11-13` is the only definition), so `ValidationStrategy.java:28-30` never breaks. Do not tell anyone validation stops at the first error.
- **Names that mean something other than they suggest.** `ValidateShipToWithAvalara` does nothing (§2.1). `ValidateOrderLockStatus` does nothing and is in no strategy. Material status is read from a field called `getEolText()` (`ValidateLineProductsForOrder.java:105`), and its Javadoc at `:94-99` claims status `40` may not be added to an order while the code's accepted list at `:101-103` **includes** `40` — trust the code. `CC_WARNING_...` is the one `INFORMATION` message while many `CC_ERR_...`/`CC_INVALID_...` keys are also errors; severity lives in `type`, not the key.
- **A rejection returns HTTP 200 and logs nothing.** A run that finds 50 errors is a `200 OK` with a populated array and, in prod, zero log lines. There is no log-based way to know a validation call happened, let alone what it found — only the access log shows the request at all.
- **Silent skips.** Of the toggle-driven skips in §3.1, only two log anything, and one of those is DEBUG (invisible in prod). `salesOpportunityEnabled`, `isShipFixedDateDelivery` and the GB/CPQ/B2B gates leave no trace whatsoever.
- **PII in log text.** Only one line prints a business value — the account number at `ValidateOneTouchPrintingSwitchboard.java:104`. Redact it when quoting.
- **Two translation bundles are empty.** `i18n/messages_en_BE.properties` (2 bytes) and `i18n/messages_en_NL.properties` (0 bytes) contain no keys, yet `ApplicationConfig.listLocales()` derives the supported-locale list from the *filenames* in `i18n/*.properties` (`ApplicationConfig.java:40-47`), so `en_BE` and `en_NL` are advertised as supported with nothing in them. `messages_en`, `messages_fr_FR` and `messages_de_DE` each carry 162 `CC_` keys. `spring.messages.use-code-as-default-message=true` (`application.properties:34`) means an unresolvable key surfaces to the user as the **raw key string** (e.g. `CC_SOME_KEY`) rather than an error — if a support ticket shows a `CC_`-prefixed code in the UI, that is a missing translation, not a code bug.
- **A fake endpoint and an empty file are in the working tree, uncommitted.** `GET /api/v2/pricelist/{accountNumber}`, described in its own comment as `// Fake endpoint to emulate the external SPT pricelist endpoint without using the DTO`, returns a hard-coded 404 whose `detail` is the non-English placeholder string `nu vrea ma vere` (`ValidationStrategyController.java:35-41`). `src/main/java/.../config/RequestLoggingFilter.java` exists but is **empty (0 bytes) and untracked**. Neither is in commit `f36e389`; both would need removing before release. Do not document either as service behaviour.
- **Working-tree config drift.** `server.ssl.enabled` is `true` in the committed `application.properties` and has been flipped to `false` locally, and `application-local.properties`/`application-sit.properties` have gained `server.ssl.enabled = false`. Also note `application-sit.properties:1` sets `env.profile=dev`, not `sit`. Verify TLS state on the host rather than from the file.
- **`management.health.db.enabled=true`** (`application.properties:31`) is set although datasource auto-configuration is excluded (`OrderValidatorApplication.java:11`). Treat any DB entry in `/health_details` as suspect and confirm on the host.
- **The 20 stubs mean absence of a message proves nothing.** For any validator listed in §3.1, "no error returned" carries no information about the order.

## 12. Escalation

| Symptom | Route | Business or fault? |
|---|---|---|
| Any message in §4 returned for a real order | **Back to the order owner / sales support** — the order data needs correcting. Do **not** raise a ticket against this service. | BUSINESS |
| `no validation strategy was found` / HTTP 400 | Caller team. Almost always the `inbound-order-auto-approval-validate` name-vs-filename trap (§3). | Fault, in the **caller** |
| Startup fails with `cannot find bean with validator name X` or `no strategy JSON file found!` | This service's owning team — a strategy JSON references a validator that does not exist, or the artifact is missing strategy files. | Fault, here |
| Fewer than nine `Loading strategy from {}` INFO lines at startup | This service's owning team — packaging problem. | Fault, here |
| "Validation missed something it should have caught" | This service's owning team, **but check §3.1 first**: name the strategy, and check whether the validator is one of the 20 stubs or is simply absent from that strategy's list. | Usually scope, not a fault |
| Message returned in the wrong language | This service's owning team — see the two locale traps in §11. | Fault, here |
| A `CC_`-prefixed code shown to the user instead of text | This service's owning team — missing translation key. | Fault, here |
| `Hostname is unknown` ERROR | Ignore. | Neither |
| `Not implemented` WARNs | Ignore — expected on every request. | Neither |
| "Who changed / submitted this order?" | **Refuse and redirect.** This service records no actor, no history and no status (§6, §10). | Wrong service |

**What to hand over.** There is no join key in this service's logs (§8), so a ticket must carry: (a) the **strategy name** sent, (b) the **full request body** or at minimum the header and the affected lines, (c) the **full response** message list, and (d) a **timestamp window with timezone** to locate the request in the Tomcat access log. Without the request body a report is not actionable — every rule in this service is a pure function of its input.
