```yaml
service: order-engine
aliases: [oe, "Order Engine", "Order Engine 2.0", orderengine]
repo: order-engine-2.0
logback_context_name: order-engine
log_files:
  - orderengine.log        # prod d:/apps/orderengine ; local c:/temp/logs/orderengine
  - order-engine-cache.log # Hazelcast cache events only
  - tomcat access log      # same directory
log_format: plain text (NOT JSON)
host_retention: 10MB rolling, maxHistory 7 (~7 days), gzipped
environments: [local, override-local, dev, sit, test, ua2, cte, preprod, prod]
correlation_id: EventId (MDC) — inbound queue listeners only, empty otherwise
primary_join_key: cartHeaderId
statuses_written: [SAVED, SUBMITTED, ORDER_REJECTED]
```

# order-engine

Every fact in this document was verified against the source. Where something could not be
verified it says so — treat those as unknown, not as true.

Sections 1–6 explain **how it works**. Sections 7–12 are for **diagnosing an order**.
Every `§` reference in this file is to this file; when citing the answering policy, name
it — "answering policy, §2.2".

## 1. What Order Engine does

Order Engine is where a Computacenter **sales order is built, priced, checked and then sent
to SAP ERP**. Orders reach it two ways:

- **A person creates one** in the Order Engine web application — picks a customer account,
  adds products, sets delivery and reference details, then saves and submits.
- **Another system sends one in** over a message queue. These can be approved automatically
  without anyone touching them.

Along the way it calls out to other services to validate the order, price it, find rebates,
resolve addresses and tax, check margin, and register it for tracking. When everything
passes it publishes the order to a SAP-bound queue and marks it **Submitted**.

Order Engine consumes inbound orders from its queues **regardless of which system sent
them** — the channel is recorded as data on the order, not decided by the queue.

### 1.1 What Order Engine is *not* responsible for

Its responsibility ends when the order is published to the SAP-bound queue. It does **not**:

- assign the SAP/ERP order number
- approve, reject or suspend an order after submission
- do ERP processing, delivery, shipping or invoicing
- control what a customer sees in a tracking portal (that is Track & Trace)
- **grant permissions** — user rights come from JAM (§5.4)

Getting this boundary wrong sends people to the wrong team.

## 2. Key concepts

| Term | What it means |
|---|---|
| **Cart header / cart item** | How an order is stored: the "cart header" is the order (customer, dates, references, totals), "cart items" are its lines. The database is a legacy Blue Martini system, which is why *cart* appears where you'd expect *order*. |
| **Cart header id** | Order Engine's internal number for the order — what appears in the logs, and so the key for tracing it technically. |
| **OE order number** | The number users quote and search on. |
| **SAP order number** | Assigned by SAP *after* submission and written back. Null until then. |
| **Order type** | **Standard** (SAP `ZGOR`) or **Contract** (`ZABR`) — a contract order draws down against an agreed customer contract. |
| **Origin / channel** | Where the order came from: `OE` (keyed in by hand), `B2B`, `SnD`, `SNOW` (ServiceNow), `HYB` (TechSource), `CPQ`, `VCT` (Salesforce — the constant `SF` is a legacy alias for the same channel). Origin controls which actions are allowed. |
| **Sourcing type** | How a line is fulfilled. Four are user-selectable: **From Stock**, **From Contract**, **Back to back (BB)**, **Direct Delivery (DD)**. Sourcing drives cost and several auto-approval blocks. (Internally "services" and "text" are also line kinds, but they are not sourcing choices.) |
| **Margin** | The commercial margin. Checked by an external service called **Checker** — too low means the order is held (§4.3). |
| **UDFs (user-defined fields)** | Extra per-customer fields configured outside Order Engine and delivered by the settings service. There are **Header UDFs** and **Line UDFs**; the Header UDFs tab appears only for customers that have them. |
| **Fees** | Environmental levies (Bebat, Auvibel, Reprobel, Recupel). Configured **per country** in a table, not per customer, and the Fees tab appears based on the order's country. |
| **Material status** | Product lifecycle flags. **20** blocks automatic approval (§4.4). End-of-life proper is **40 and 50**. A status-50 product is still selectable until its display-until date expires. |
| **Partner functions** | Roles on an order: Sold-To, Bill-To, Payer, Ship-To, end customer, plus account manager, sales assistant, second payer and courier. |
| **Blocking reason (auto-approval)** | A configured per-customer rule saying "orders like this never go straight through" — a deliberate business control, not an error (§4.5). |
| **`ZM` blocked flag** | Something different: the marker stamped on an order the user chose to submit *despite* failing the margin check, so SAP holds it (§4.3). |
| **Auto-approval** | The automatic decision, for queue-delivered orders, about whether an order can go straight to SAP (§4.4). |
| **Regenerate** | Throw this order away and rebuild a fresh one from the original inbound message (§5.2). |

### 2.1 Concepts this document does not cover

The commercial meaning of the following has **not** been verified. If asked, say you don't
have a reliable definition and offer to find out — **do not infer meaning from the name.**

- Bundles and parent/child lines; what a parent line's price represents
- Line folders and grouping
- The individual price fields (list, sale, extended, requested, discount, override)
- `ZBUN` and any SAP `Z*` code beyond `ZGOR`, `ZABR`, the `ZM` blocked flag and the `ZG06`
  text code
- Contract call-off vs internal contracts, and `CONTRACT_LOCKED_ATTEMPTING_RETRY`
- Rebate scheme mechanics and PVC
- How CC company, Salesforce level two and organization identifier combine to resolve a
  customer's settings
- Services answers on an order

## 3. How an order flows

Everything a user does goes through **one action** with a different setting each time. The
steps run in this order: validate → duplicate PO → address/tax → margin → save → tracking →
send to SAP.

| What the user did | Validate | Dup. PO | Address/tax | Margin | Save | Tracking | To SAP |
|---|---|---|---|---|---|---|---|
| **Validate Order** (edit or view) | yes | – | yes | – | – | – | – |
| **Save** (new order) | yes | – | – | – | yes | yes | – |
| **Save** (existing order) | yes | – | – | – | yes | yes | – |
| **Save & Submit** (new or existing) | yes | yes | yes | yes | yes | yes | yes |
| **Submit** (from the read-only view) | yes | yes | yes | yes | **never saves** | yes | yes |
| **Inbound order, no auto-approval** | – | – | – | – | yes | yes | – |

**Inbound order with auto-approval runs in a different order** and does not fit the table:
the order is **saved first, unconditionally**, then registered for tracking, and only then
judged by the six auto-approval rules (§4.4). It is sent to SAP only if all six pass.

### 3.1 Why a step gets skipped — and why "nothing was saved"

Each step is gated separately; there is no single early exit. The order is **not saved** when:

- every validation message is an error, or
- the duplicate-PO confirmation is still outstanding, or
- the margin check returned failures, or
- address/tax validation ran and returned no tax-exemption result (US orders only — §4.6).

This is the usual explanation for **"I pressed Save & Submit and nothing was saved."** It is
by design: Order Engine will not persist an order it is about to refuse.

**Two counter-intuitive details.** There is no "warning" level — validation messages are
either **ERROR** or **INFORMATION**. A *single* message of any kind suppresses the
duplicate-PO check, the address/tax step and the margin check. But because the save gate
requires *every* message to be an error, a **mix of errors and information messages means
the order IS saved despite containing errors.**

## 4. The rules that stop an order

### 4.1 Order validation

An external rules service checks the order and returns messages tagged per tab, field and
line. The UI highlights offending tabs in red:

> *"You have either entered invalid data or not entered any data in one of the required
> fields. The field requiring your correction is marked with an exclamation point (!)."*
> *"One or more tabs contain problems. The tabs have been highlighted in red."*

### 4.2 Duplicate customer order reference

Checked **only for customers whose `duplicatePo` setting is `2`**; for anyone else the check
is skipped entirely. If the same reference is already on another order for that
**organisation**, the user is asked:

> *"The customer order reference has been used in another order, do you still wish to
> continue?"*

Confirming re-submits with an approval flag, and the check is not repeated. **This is
self-service — not a colleague's approval.**

### 4.3 Margin check (the "Checker" service)

Checker validates the order's **pricing and margin rules** — it is *not* compliance or
export control. Only failures come back. Any failure stops save, tracking and submission:

> *"Your order had failed margin validation check. Proceeding with submission of your order
> will result in the order being blocked in SAP. Please see below for details"*
> *"Please enter reason text if you need to continue:"*

**The reason text is an override.** Supplying one skips validation, duplicate-PO, address/tax
and margin, and goes straight to save → tracking → submit. The outgoing SAP order is stamped
with blocked reason **`ZM`** plus a text (code `ZG06`) carrying the failed validations and
the user's justification. So **the order reaches SAP deliberately blocked, with the reason
attached, for a person in SAP to release.** It is not stuck in Order Engine, and Order Engine
cannot release it.

Separately, Order Engine shows its own advisory low-margin warning when the total falls
strictly below a per-organisation threshold: *"Margin is lower than threshold"*.

### 4.4 Auto-approval — queue-delivered orders only

Two things matter most:

1. **The order is created first, always** — before it is judged.
2. **It is created inactive, and order search only ever returns active orders.** So an order
   that failed auto-approval **cannot be found through the UI search at all**, even though
   the row exists. This is the single most confusing consequence of a failed auto-approval.

Every failure writes **a plain-language reason onto the order as a header text**. *That text
is the first thing to read* — it names the rule that stopped the order.

Six rules run in order and stop at the first failure:

| # | Rule | Declines when | Reason the user sees |
|---|---|---|---|
| 1 | Order validation | the rules service objects, or the ship-to address had to be corrected or could not be validated | "Order has failed validation" |
| 2 | Duplicate customer PO | the same reference is already on a live order (Submitted / Approved / Contract locked) for that customer. **Also declines if it cannot get a lock after 3 tries**, so two simultaneous orders with the same PO can never both auto-approve. Skipped entirely unless the customer's `duplicatePo` setting is `2` | "Order requires duplicate PO validation" |
| 3 | Margin | Checker returns failures, **or the Checker call itself fails** | "Order margin threshold requires approval" |
| 4 | Forced manual approval | the sending system set a flag on this order to opt it out | "Inbound order has force manual approval flag set." |
| 5 | Material status | any line has a product at material status 20 | "Order has order lines with materials at material status 20." |
| 6 | Blocking reasons | any configured rule matches (§4.5) | one combined message listing **every** reason that matched |

On success the order is submitted to SAP, made active, and stamped as last modified by the
pseudo-user **`AutoApprove`** — so "who changed this?" is *nobody*, it was automatic. (The
stamp only lands if a real account named `AutoApprove` exists; otherwise no user is recorded.)

If an external service throws while auto-approving, the failure is recorded as the generic
*"Order has failed validation"*, **which masks the real cause.**

Whether a given channel is eligible for auto-approval at all is decided **upstream**, not
in Order Engine — it judges whatever arrives on its approval queue.

### 4.5 Blocking reasons

A configured business control **per customer**: the order is valid, but the business wants a
person to look at orders of this shape. All rules are evaluated and every match is listed.
Numeric thresholds are **"greater than or equal to"** — a limit of 10,000 blocks an order of
exactly 10,000.

| Rule | Blocks when | Message |
|---|---|---|
| Mixed sourcing | the order uses two or more sourcing types | "Blocked for multiple sourcing methods." |
| From Stock / BB / DD / From Contract | any line uses that sourcing type (four independent switches) | "Blocked for from stock sourcing." / "…for BB sourcing." / "…for DD sourcing." / "…for From Contract sourcing method." |
| Delivery address override | the ship-to address was overwritten | "Blocked for delivery address override." |
| Future delivery date | delivery is N or more days ahead (skipped if the setting is absent or zero) | "Blocked for future delivery date timeframe greater than or equal to {N} days." |
| Max order value | order total reaches the limit | "Blocked for maximum total order value greater than or equal to {X}." |
| Max line value | any line total reaches the limit | "Blocked for maximum order line value greater than or equal to {X}." |
| Max line quantity | any line quantity reaches the limit | "Blocked for maximum order line quantity value is greater than or equal to {X}." |
| Max DD suppliers | too many distinct direct-delivery suppliers | "Blocked for maximum number of DD suppliers is greater than or equal to {X}." |
| Specific SAP materials | the order contains a listed part number | "Blocked for due to SAP materials: {list}." |
| Material attributes | a line matches a configured manufacturer / product-class rule | "Blocked due to SAP material(s): {part} having attributes {…}" |
| Currency mismatch | the inbound order's currency differs from the customer's (bill-to address currency, falling back to the organisation's). **Always runs — there is no switch for it** | "Currency {A} on inbound order is not same as currency {B} on customer account data." |

**Important scope limit:** delivery lines, service materials, bracket items, text lines and
bundle parents are excluded **only from the Mixed-sourcing and From-Stock rules**. For BB, DD
and From Contract, nothing is excluded.

### 4.6 Address and tax (Avalara) — US orders only

Avalara runs **only when both the order country and the ship-to country are US.** For every
non-US order the whole step is a no-op — which also means the "no tax-exemption result" save
blocker in §3.1 can never fire outside the US.

The auto-approval path checks the **address only**, not tax exemption. Users see:

> *"Ship-To Address validation: address is invalid."*
> *"Ship-To address must be validated."*
> *"Address validation - Unexpected error happened. Please retry - If the problem persists
> please log an incident on NGSD. If you choose to submit without validation, please ensure
> the Ship-to address is correct otherwise Tax calculation may be incorrect."*

## 5. What users can do

### 5.1 The journey through the application

Order search → open an order, or pick a customer account and create one → the order screen,
with tabs in this order: **Header**, **Header UDFs** (only if configured), **Quick add /
pricing**, **Rebates**, **Costs & Sourcing**, **Text/Other**, **Blocking & Grouping**,
**Fees** (only if configured for the country) → Validate → Save → Submit.

Products come from the catalogue (a product search screen, or a "Quick Add" box where a user
pastes part numbers), or by **uploading a spreadsheet**. Order lines can be **downloaded as
a spreadsheet** — note that downloading requires **edit** rights, not just read.

Product search returns at most 250 results (configurable): *"Your search result set is too
large to display in full. Only the first 250 are shown."*

### 5.2 Regenerate

Regenerate does **not** edit or copy the order. It asks the integration layer to **replay the
original inbound message** and build a brand-new order, then **rejects the old one** with the
reason *"Order regenerated"*. The user gets a **new order number** and is taken to it.

Only for **B2B, SnD and ServiceNow** orders in status Saved, Submitted, Approved, Rejected or
Contract-locked. **TechSource orders can be rejected but never regenerated.**

The confirmation warning depends on status. Saved and Rejected orders get no SAP warning at
all; Submitted and Approved orders do, the Approved one naming the SAP order:

> *"Before continuing, please ensure that SAP order {number} has been cancelled. Please
> confirm you wish to reject this order and regenerate a new version of the order from the
> original inbound order payload."*

**Timing.** Order Engine waits about **10 seconds** (5 attempts, 2s apart), scaled up for
large orders by line count. If the new order isn't ready the user sees:

> *"Order regeneration is taking longer than expected and will continue in the background.
> Please check back later."*

…and is returned to order search. Work continues in the background for roughly **5 minutes**
(also scaled by line count). **The old order is only rejected once the new one actually
arrives** — so if everything times out, the original order is left untouched and nothing
further is reported. That is the explanation for "I regenerated and nothing happened."

Whether the rebuilt order is re-priced with today's data happens in the integration layer and
is **not verifiable from Order Engine** — don't claim it either way.

### 5.3 Reject

Available on **Saved** orders from B2B, SnD, ServiceNow or TechSource. A reason is mandatory
(the 1024-character cap is enforced only by the UI). The order becomes **Order Rejected**
in the order view, order search and Track & Trace, with the reason attached.

Who gets told depends on the customer: if a B2B customer rule exists, a full status record
goes to that customer's own system; otherwise the **Salesforce case is cancelled**.
Regenerate-driven rejections always cancel the Salesforce case. A Salesforce outage does not
fail the rejection.

**Rejecting is not deleting.** The order remains and can still be regenerated. Deleting is
separate, and only for Order Engine, CPQ and Salesforce orders in Saved status.

### 5.4 Why a user "can't do something" — three causes

1. **Missing rights.** The two that matter are **`login`** (read-only: search, open and read
   orders) and **`crud`** (everything that changes anything — create, edit, save, submit,
   reject, regenerate, delete, change currency, add lines, and spreadsheet download).
   Without `crud` the whole action-button block is hidden. A third right, `block_order`,
   reveals one extra control on the Header tab, so two `crud` users are not necessarily
   identical. **Order Engine cannot grant rights** — they come from JAM. Users hitting the
   401 page see *"User is not authorised to visit this page. Please raise a request through
   NGSD if access is required."* The answer is always: raise an NGSD request. (Note: a user
   who merely lacks `login` is silently redirected back to order search with **no message**,
   which looks like a broken link rather than a permissions problem.)
2. **Someone else is editing it.** Opening an order for editing locks it for **15 minutes**.
   Others see *"Order action not possible - order currently being edited by user
   {username}"* and the buttons are disabled. The lock **cannot be taken from another user**
   and there is no force-release — it expires by itself. Waiting is the answer.
3. **The order's status or channel doesn't allow it.** Submit requires status **Saved**. An
   order in *Order Awaiting Approval* can still be opened and edited but **cannot be
   submitted** — the most likely cause of "why is Submit greyed out?". Users may also see
   *"Order no longer at status saved"*. Channel restrictions mostly hide buttons silently,
   with no explanation; the one visible message, *"Not available as this is a TechSource
   order"*, is the tooltip on a **disabled Change Sold-To Account button**, not a hidden
   action.

### 5.5 There is no approver in Order Engine

**No approver role, no approval queue, no approval screen.** Approval happens elsewhere:

- **Automatically**, for queue-delivered orders (§4.4).
- **Self-service**, where the same user confirms a duplicate PO or types a margin reason.
- **In SAP**, for anything shown as *Order Awaiting Approval* or *Order Approved*, and for
  releasing a `ZM`-blocked order.

Order Engine never holds an order for a colleague's sign-off.

## 6. Statuses, and who writes them

| Status | Display text | Written by |
|---|---|---|
| `SAVED` | Saved | **order-engine** |
| `SUBMITTED` | Submitted | **order-engine** |
| `ORDER_REJECTED` | Order Rejected | **order-engine** (its own reject/regenerate action) *and* downstream |
| `ORDER_AWAITING_APPROVAL` | Order Awaiting Approval | downstream SAP/BTP |
| `ORDER_AWAITING_AUTO_APPROVAL` | Awaiting Auto Approval | downstream SAP/BTP |
| `ORDER_APPROVED` | Order Approved | downstream SAP/BTP |
| `ORDER_SUSPENDED` | Order Suspended | downstream SAP/BTP |
| `REJECTED_AUTO_FIX` | Rejected auto fix | downstream SAP/BTP |
| `CONTRACT_LOCKED_ATTEMPTING_RETRY` | Contract locked, attempting retry | downstream SAP/BTP |

**Order Engine writes exactly three statuses: `SAVED`, `SUBMITTED` and `ORDER_REJECTED`.**
Never attribute any other status to it.

Re-submission is refused for orders already `SUBMITTED`, `ORDER_APPROVED` or
`CONTRACT_LOCKED_ATTEMPTING_RETRY` — the double-submit guard, correct behaviour not a fault.

### 6.1 Auto-approval outcome codes

`0` not eligible · `10` failed validation · `20` duplicate PO needs approval · `30` margin
needs approval · `40` blocked by a configured reason · `50` auto-approved.

### 6.2 Track & Trace milestones (customer-visible)

`290` failed · `300` created · `320` saved · `330` deleted · `340` submitted to ETL · `350`
submitted to ERP · `390` rejected.

The milestone record lives in Track & Trace, but **Order Engine pushes 300, 320, 330 and 390
into it** — so it is not purely a downstream concern.

---

## 7. Log anatomy — read this before quoting any line

Config: `oe-backend/src/main/resources/logback-spring.xml`. File pattern:

```
-%d{-yyyy-MM-dd HH:mm:ss.SSS} -%5p ${PID:- } [EventId: %X{EventId}] --- [%t] %-40.40logger{39} : %m%n-%wEx
```

A real line:

```
--2026-07-28 11:54:03.812 - INFO 4812 [EventId: ] --- [http-nio-8052-exec-7] c.c.o.service.OrderSubmitService         : Submitted order:8845213
```

Parsing traps — all normal, none are faults:

- **Lines begin with `--`** (a literal `-` plus another starting the date format).
- **The level is padded after a dash**: `- INFO`, `-ERROR`, `- WARN`.
- **`[EventId: ]` empty is the normal case** — see §11.1.
- **Logger names are abbreviated and hard-truncated to 40 characters**, so
  `c.c.o.service.OrderSubmitService`. Match on the class name, not the package.
- **Stack traces start on the *next* line**, blank-line-wrapped with `\tat …` frames,
  `Caused by:` chains and `… N common frames omitted`. Join multi-line events on the next
  `--<date>` line start, **not** on indentation.

### 7.1 Level availability per environment — critical

`com.computacenter.orderengine` logs at **DEBUG everywhere except `prod`, where it is INFO**.
In prod, Hibernate is at ERROR, and so are `org.springframework.web` and
`org.springframework.security` — but **not** Spring as a whole, so e.g. `org.springframework.amqp`
still logs at INFO.

Live-code statement counts: **146 `log.debug` against 31 `log.info`** (16 `warn`, 74 `error`,
6 `trace`). So **roughly 80% of the flow narration does not exist in production logs.**
Treat anything marked DEBUG below as unavailable in prod and apply the absence rule.

### 7.2 Retries — sometimes duplicate lines, sometimes not

REST clients retry **3 attempts total, 1 second apart**. But retry fires **only on HTTP 404
and 503, plus connection failures.** Every other status fails immediately.

So: three identical `Error while calling Validator with status code 503` lines a second apart
are **one** logical failure. A **500 or 502 logs exactly once** — collapsing it, or claiming
it was retried, would be wrong.

There is **no 30-second time limit** in force (the config exists but nothing applies it).
The real limits are 15 seconds to connect and 15 seconds to read, per attempt.

### 7.3 Retention and file names

10MB rolling files, 7 days, gzipped. The prod file is
**`d:/apps/orderengine/orderengine.log`** (no hyphen); the cache log keeps the hyphen,
`order-engine-cache.log`. The Tomcat access log sits in the same directory.
<!-- TODO: confirm the Elasticsearch cluster's own retention. -->

## 8. Identifiers in the logs

| Identifier | How it appears | Join scope |
|---|---|---|
| `cartHeaderId` / `headerId` / `orderHeaderId` | free text — `cart header id: 8845213`, `CartHeaderId 8845213` | **primary search key** |
| OE order number | `Get order by Order Number:…`, regeneration messages | the number users quote |
| `EventId` | the MDC slot `[EventId: …]` | inbound queue journeys only |
| AMQP failure `correlationId` | random UUID per failure, in the error-handler line only | joins that line to the parked message |
| `accountNumber` | `Order was created successfully for account : …` | customer, not order |
| SAP order number, `tatGuid`, Salesforce refs | rarely logged; live in the database | downstream/upstream joins |

Searching is **substring matching on the message text**.

## 9. Journey stages and their log evidence

Messages in backticks are **verbatim**, including odd spacing; `{}` marks a substituted value.
The logger named is the one that emits it — match on that class.

| # | Stage | Success evidence | Failure evidence | A stall here means | Next check |
|---|---|---|---|---|---|
| 1 | Inbound order received | DEBUG `IoCreateOrderListener` — `Create Order event received from application: {} with payload {}`; for the approval queue, DEBUG **`IoOrderAutoApprovalListener`** — `Auto approval order event received from application: {} with payload {}` | ERROR `CustomRabbitListenerErrorHandler` (row 8) | the message never arrived, or the listener is stopped | listener status — **if the four inbound listeners are stopped, no queue-delivered orders arrive at all** |
| 2 | Inbound order created | DEBUG `StrategyCreateInboundOrderService` — `Inbound order with cart header id: {} was created successfully` | see row 8 | nothing persisted | `cart_header` for that id |
| 3 | Validation | no dedicated success line | ERROR `OrderValidatorService` — `Error while calling order validator system for strategy name: {}` (with stack trace); plus ERROR **`OrderValidatorClientConfiguration`** — `Error while calling Validator with status code {}` (§11.4) | the rules engine rejected the order or was unreachable | Order Validator service logs |
| 4 | Duplicate customer PO | no success line | **WARN** `AutoApprovalDuplicateCustomerPoNumberStep` — `Order with customerReference {} and cartHeaderId {} already exists in SUBMITTED/CONTRACT LOCKED/APPROVED status` | **business block, not a fault** (§4.2) | the existing order with that reference |
| 5 | Address / tax (**US only**) | no success line | ERROR `AvalaraService` — `An error occurred during avalara validation of ship to address: {}` (**substitutes the whole address — redact, §11.8**), `Unexpected error happened during avalara address validation`, `…for auto approval`, `An error occurred during avalara get company data`; ERROR `AvalaraClientConfiguration` — `Avalara 5xx error` (**no placeholders at all**); DEBUG `Avalara returned 4xx - body contains error details, deserializing as response` | address unresolved or tax-exemption lookup failed | Avalara (AvaTax) — third party |
| 6 | Margin (Checker) | **INFO** `CheckerService` — `Cart Checker client responded successfully on verification with id : {}` — **the value is the *count of checks*, not an id** (§11.9) | outage: ERROR `CheckerService` — `Error while calling check order margins for order with customer reference {} ` (trailing space; customer data — redact); ERROR `CheckerClientConfiguration` — `Error while calling Checker with status code {}` | a margin *failure* produces no error line — it goes back to the user as a dialog (§4.3) | whether the submitted order carries blocked reason `ZM` |
| 7 | Auto-approval | **INFO** `StrategyAutoApproveInboundOrderService` — `Inbound order with cart header id: {} was auto approved successfully` | DEBUG `Inbound order with cart header id: {} failed auto approval`; ERROR `Error on Auto-approval for order with cart header id {} while calling an external service with exception {}.` (**message only — no stack trace**); per-step DEBUG `AutoApproval<X>Step failed for order with headerId {}. Error: {}` — **except rule 6, which logs `AutoApprovalBlockingReasonStep failed for order with headerId {}. Reason to block: {}`** | one of the six rules declined it — **step detail is DEBUG, invisible in prod** | **the header text on the order names the exact reason** (§4.4); also the auto-approval code (§6.1) |
| 8 | Order saved | **INFO** `OrderService` — `Order was created successfully for account : {}` (account, not order id) | — | nothing persisted | status should be `SAVED` |
| 9 | Track & Trace | no success line | ERROR `TrackingTraceService` — `Error while calling create tracking system`, `Error while calling save tracking system`, `Error while calling tracking and trace update: `, `Tracking trace update for order reject failed: `; plus `Error while calling TrackingTrace with status code {}` | **visibility is broken, not the order** — it can still reach SAP. Do not say the order failed | read the real state from the order's status |
| 10 | **Submit to SAP queue** | **INFO** `OrderSubmitService` — `Submitted order:{}` (no space after the colon) | absent, plus the message in `order_create_sap_error` | built but never published to SAP | `order_create_sap_error` |
| 11 | Publish (any queue) | DEBUG `AmqpPublishService` — `Event sent to routing key {} with payload : {}` | see row 12 | — | that routing key's `*_error` queue |
| 12 | Queue processing failure | — | **ERROR** `CustomRabbitListenerErrorHandler` — `Exception occurred during message processing. Check error queue {} for correlationId {}. Exception: ` + stack trace | parked; **it will not retry itself** | the named `*_error` queue and its `x-cc-failure-*` headers (§10.1) |

### 9.1 Lines that look like errors but are not

- DEBUG `OrderSubmitService` — `Not re-submitting order {} in status Submitted, Contract locked or Approved.` — the double-submit guard working correctly (§6).
- ERROR `ObjectLockService` — `Unable to unlock Order with CartHeaderId {}` — may leave a stale edit lock, which clears itself after 15 minutes (§5.4).

### 9.2 Regenerate leaves almost no trace

`Regenerate Order process for cartHeader Id: {} starting`, `Waiting for order regeneration on
temporary queue name {} for event Id: {} after {} retries` and `Regeneration process finished
successfully and new order with order number {} was generated` are **all DEBUG**. In prod a
regeneration that timed out is essentially invisible — diagnose it from state instead: the old
order still present and not rejected means the new order never arrived (§5.2).

### 9.3 Queue topology

Exchange `order-engine-exchange` (topic). Vhosts `/order_engine` and `/inbound_order`; quorum
queues.

| Queue | Routing key | Error queue |
|---|---|---|
| `order_created` | `order.created` | `order_created_error` |
| `order_create_sap_q` | `order.create.sap` | `order_create_sap_error` |
| `order_data_ready` | `order.data.ready` | `order_data_ready_error` |
| `order_regenerate` | `order.regenerate` | `order_regenerate_error` |
| `order_manual_reject` | `order.manual.reject` | `order_manual_reject_error` |
| `order_regenerated_ready` | durable, consumed by the fourth inbound listener on `/inbound_order`; it declares the short-lived reply queues `order_regenerated_ready_temporary_q_…` on `/order_engine`, which expire after 6 minutes | — |
| `order_init`, `order_approval`, `order_create` | consumed only (`/inbound_order`) | — |

There is also a fanout exchange `order-engine-error-dlx`, but note it is **not** the
dead-letter exchange of the main queues — each main queue dead-letters via the default
exchange straight to its own `*_error` queue, and the five error queues are bound to the
fanout.

## 10. Evidence that is not in the log file

### 10.1 The `*_error` queues — the richest error record

A failed message is republished to `<queue>_error` with these headers — **lowercase in the
message, whatever the Java constant names suggest**:

`x-cc-failure-message` (JSON), `x-cc-failure-exception`, `x-cc-failure-stacktrace`,
`x-cc-failure-timestamp`, `x-cc-received-routing-key`. Two more ride along from the inbound
message: `x-cc-producer`, `x-cc-authentication` (**treat as a secret — never quote it**).

Match the `correlationId` from the ERROR log line to the parked message. The mechanism is a
republish, not a broker dead-letter, and the message is **not** retried automatically.

### 10.2 Database tables

| Table | Holds |
|---|---|
| `order_queue` | ERP request **and** response payloads, an order-details payload, status, timestamps, server — the best view of the SAP round trip. Status codes are free text with no enforcement; the source comments them as `S` submitted, `X` no response, `N` initial save, and `P` unexplained |
| `b2b_order_audit` | the **full inbound payload**, keyed by event id (cart header id is a lookup column, not part of the key). Order Engine reads only the event id from it — the payload itself is replayed by the integration layer |
| `b2b_order_status` | status, reject comments, order message, SAP order number, partner numbers |
| `custom_order_search` | current status, SAP order number, all reference numbers. **Only active rows are returned by order search** (§4.4) |
| `order_tracking` | tracking status, ERP order number, delivery **contact**, email-sent flag |
| `LISTENER_STATUS` | per-node inbound listener RUNNING/STOPPED |

**These are current-state snapshots, not history** — status is overwritten in place. They give
milestone timestamps, never an ordered event stream.

### 10.3 Non-log evidence unique to Order Engine

- **The order's header texts.** Auto-approval writes its decline reason there in plain
  language. For any inbound order that didn't go through, this is the best source — better
  than the logs, which record the same thing only at DEBUG.
- `order-engine-cache.log` — Hazelcast entry events keyed by cart header id; a de-facto
  per-order "was touched" log, useful when the main log is silent.

## 11. Blind spots and traps

### 11.1 There is almost no correlation id

`EventId` is set only by the four inbound queue listeners, from the message metadata.

- **Any order started in the UI has an empty `[EventId: ]`.** Normal.
- **Nothing propagates over HTTP** — no request id, no trace id, no OpenTelemetry.
- The MDC is **not cleared in a `finally` block**, so an `EventId` can leak onto a later
  unrelated line from the same pooled consumer thread. (The failure path does clear it, so
  the window is narrow.) Corroborate with `cartHeaderId` before treating two lines as the
  same order.
- MDC is **not propagated to async threads**, so regenerate's background polling logs with no
  EventId at all.

### 11.2 Never say who did something

`@CreatedBy` / `@LastModifiedBy` are meaningless — the auditor returns a hardcoded constant
with a `TODO` in the source. If asked "who changed this order?", decline and explain that
user attribution is not recorded. (Exception: auto-approved orders are stamped `AutoApprove`,
which means *nobody*.)

### 11.3 A validation rejection can look like a success

The handler for bean-validation failures returns **HTTP 200** with a validation body **and
logs nothing**. Never conclude from a 200 that the order was accepted. (Constraint violations
*are* returned as 400 — this applies to the argument-validation path specifically.)

### 11.4 The shared downstream-failure line carries no order id

All eight REST clients share one line, emitted by **`<Service>ClientConfiguration`** — not by
the service class:

```
Error while calling {} with status code {}
```

Service names come from a fixed set: `Validator`, `Checker`, `TrackingTrace`,
`StrategicPricingTool`, `OrderSettings`, `Salesforce`, `RSM`, `JAM`. Avalara does **not** use
it (§11.8).

**It records the service and HTTP status only — no order id.** Attribute it via the thread
name plus a tight timestamp window against that order's other lines, and **say that you did**:
"attributed by thread and time, not by order id."

### 11.5 Stack traces are inconsistently captured

An exception reaches the log only when the code passes the throwable. Several places pass
only `e.getMessage()` (including the auto-approval catch-all), and `Avalara 5xx error` passes
nothing at all. A message-only ERROR does not mean the exception was trivial.

### 11.6 Timestamps and hosts

No timezone or offset is recorded, and the app runs on multiple nodes. Don't order events
across services by timestamp alone.

### 11.7 One stale code tree exists — never cite it

The **`client/feign/**`** package is untracked by git and imported by no live service. Its
message texts differ subtly from the live `client/rest/**` ones — for example
`…for order with number {}` where the live line says `…for order with customer reference {}`.
Quoting from it sends a log search after strings that are never emitted. The live REST clients
are the ones under `client/rest/**`.

### 11.8 Avalara lines print the customer's address — redact them

Avalara's own lines are listed in §9 row 5. The first substitutes **the entire address
object**. Never echo it — say "the ship-to address" and redact street, postcode and name.
None of these lines carries an order id, so §11.4's attribution caveat applies. On `Avalara
5xx error`, check whether other orders failed in the same window before blaming the order's
data — it is usually a third-party outage.

### 11.9 The Checker "success" line is misleading, and a margin block logs nothing

The INFO line substitutes **the number of checks returned, not an id**. Never report it as one.

More importantly, **a margin failure produces no ERROR and no WARN** — it goes back to the
user as a dialog (§4.3). The absence of a Checker error does **not** mean the order passed the
margin check; look for blocked reason `ZM` on the submitted order. Distinguish this from a
Checker *outage*, which is a different answer and a different owner.

### 11.10 Do not confuse the three "block" concepts

| Concept | What it is |
|---|---|
| **Blocking reason** (auto-approval) | a configured per-customer control that sends a queue-delivered order for human review (§4.5) |
| **`ZM` blocked reason** | the flag on an order the user submitted despite a failed margin check, so **SAP** holds it (§4.3) |
| **Delivery / order block** | a header field on the order, unrelated to either |

## 12. Escalation

| Failure area | Goes to |
|---|---|
| Order Engine itself (submit, save, regenerate, locks) | Order Engine team — `TODO: team name` |
| Validation rules | Order Validator team — `TODO` |
| Margin rules (Checker) | `TODO` — a margin *block* is a business outcome, not a fault |
| Pricing / rebates (SPT, RSM) | `TODO` |
| Address / tax (Avalara, US only) | third-party AvaTax via `TODO` |
| Tracking record (Track & Trace) | `TODO` |
| SAP / ERP, any status beyond `SUBMITTED`, and releasing a `ZM`-blocked order | SAP / BTP team — `TODO` |
| Stuck message in an `*_error` queue | Order Engine team (replay) — `TODO` |
| **User needs access or a role** | **not a fault — raise an NGSD request** (§5.4) |
