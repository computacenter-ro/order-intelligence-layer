```yaml
service: "jam-ws"
aliases: ["JAM", "JAM 2.0", "Jam v2", "Java Authorisation Management", "jam-2.0"]
repo: "jam-2.0"
log_files: ["d:/apps/jam-ws/jam-ws.log (dev, sit, test, prod)", "c:/temp/logs/jam/jam.log (local)"]
log_format: "plain text (pattern inherited from Spring Boot defaults; not defined in this repo)"
host_retention: "not determinable from this repo — no rolling policy is configured here"
environments: ["local", "dev", "sit", "test", "prod", "junit (tests only)"]
correlation_id: "none — there is no MDC.put anywhere in the repo, on any path"
primary_join_key: "the caller-supplied username / samAccountName string"
statuses_written: []   # this service has no status field and writes no rows
```

# jam-ws

## 1. What this service does

jam-ws answers one question for other Computacenter applications: *what is this user allowed to do in your
application?* A calling application (Order Engine, Sales Pricing Tool) sends a username and its own
application key over HTTP Basic auth, and jam-ws returns a list of privilege names such as `view`, `admin`,
`overlay_price`, `standard_price`, `connect_price` (`initial-insert.sql:2-6`). The caller then uses that
list to decide which screens and buttons to allow.

It derives the answer by combining two sources. Azure Active Directory supplies the groups the person
belongs to; a small SQL Server database supplies the mapping from group name → role → privileges, per
application (`UserService.java:56-73`). A per-user override table can short-circuit the AAD lookup entirely
(`UserService.java:50-54`). Two applications are seeded: `oe` (Order Engine) and `spt` (Sales Pricing Tool)
(`initial-insert.sql:33-34`).

### 1.1 What jam-ws is *not* responsible for

- **Authenticating the end user.** jam-ws never sees the end user. It authenticates the *calling
  application* with a shared HTTP Basic username/password (`WebSecurityConfig.java:32-33`), and takes the
  end user's name as a plain path variable (`UserController.java:26`, `:34`).
- **Enforcing the privileges.** Its responsibility ends when it returns the JSON response. Whether the
  caller honours `admin` or ignores it is entirely the caller's concern.
- **Managing AAD group membership.** Groups are read from Azure AD and never written
  (`GraphApiService.java:36-64` — both methods only `get()`; there are no writes in the repo).
- **Maintaining the role/privilege data.** No code in this repo writes to any table — there is no `.save(`
  or `.delete` call anywhere in tracked source. The seed rows are inserted once by Liquibase
  (`changelog-initial-table-creation-insert.xml:77-79`); anything after that is changed outside jam-ws.
- **Being an admin UI.** See §5 — the bundled Angular page has no working actions.

## 2. Key concepts

| Term | What it actually means in code |
|---|---|
| `application` (path variable) | Matched against `application.name`, not the primary key, via `findByName` (`ApplicationRepository.java:10`, used at `UserService.java:59`). Seeded rows happen to have `id` = `name` (`initial-insert.sql:33-34`), so the distinction is invisible today. |
| Role | A row in `role` whose `name` is compared for **exact string equality** against an Azure AD group's `displayName` (`UserService.java:68`). Seeded names contain spaces and mixed case, e.g. `Order Engine 1.5`, `OE 2.1 UAT-Team`, `SPT-Admin` (`initial-insert.sql:8-14`). |
| Privilege | A row in `privilege`; only its `name` is ever returned (`UserService.java:53`, `:71`). |
| Authorities | The response payload — a `Set<String>` of privilege *names*, deduplicated across all matching roles (`UserService.java:67-72`). |
| Per-user override | A row in `user_application_role` keyed on (`user_name`, `application_id`) (`changelog-initial-table-creation-insert.xml:69-72`). When present it wins and Azure AD group membership is not consulted at all (`UserService.java:37-38`, `:43-44`). |
| samAccountName | The on-premises account name. Azure AD is searched with `$search` = `"OnPremisesSAMAccountName:<value>"` plus header `ConsistencyLevel: eventual` (`GraphApiService.java:29-32`, `:51-54`). |
| `UserProfile` | The `/api/user/...` response record: `username`, `email`, `firstName`, `lastName`, `displayName`, `jobTitle`, `officeLocation`, `authorities` (`UserProfile.java:6-15`). |

### 2.1 Concepts this document does not cover

Decline rather than infer on any of these — none could be verified in this repo:

- **`cc-standardized-web-app` internals** (`jam-backend/pom.xml:63-66`): `CcStandardizedWebApp`,
  `StandardizedWebSecurityConfig`, `StandardizedControllerExceptionHandler`, `ExceptionMessageDTO`. That
  artifact is not present locally and its parent pom (`pom.xml:6-10`) does not resolve. Consequently the
  **JSON shape of any error response body**, the **Tomcat access-log file name and pattern**, and any
  **additional security filter chains** are unknown.
- **The log line pattern, the rolling policy, retention, and whether a timezone is recorded.**
  `logback-spring.xml:6-8` includes Spring Boot's `defaults.xml` / `console-appender.xml` /
  `file-appender.xml` and defines no pattern of its own; the Spring Boot version comes from the
  unresolvable parent, so no pattern string or example line can be quoted honestly.
- **Microsoft Graph SDK built-in behaviour** (retry, backoff, throttling, default timeouts). Nothing in
  this repo configures it — see §7.2.
- **Liquibase's own tracking tables.** Liquibase is enabled (`application.yml:10-12`) but no table names
  are declared here.
- Whether `displayName`, `jobTitle` and `officeLocation` are actually populated in the response.
  `UserMapping.java:13-18` explicitly maps only `firstName`, `lastName`, `email`, `username` and
  `authorities`; the other three would come from MapStruct's implicit mapping, and no generated
  `UserMappingImpl` exists in the tree to confirm it.
- Anything about Order Engine's or SPT's own internals.

## 3. How work flows through this service

Two endpoints, both `GET`, both under `/api` (`UserController.java:18`). They take **different paths**, and
the difference matters.

**`GET /api/authorities/{application}/{userName}`** (`UserController.java:24-30`)

1. Look up the override row for (`userName`, `application`) (`UserService.java:37`, `:51`).
2. If found → return that role's privilege names. **Azure AD is never contacted** and the user is never
   checked for existence (`UserService.java:52-53`).
3. If not found → fall through to the whole `/api/user` flow below and return just its `authorities`
   (`UserService.java:38`).

**`GET /api/user/{application}/{username}`** (`UserController.java:32-38`)

1. Search Azure AD for the user by samAccountName (`UserService.java:42` → `GraphApiService.java:50-64`).
2. If no match → throw `UserNotFoundException` → HTTP 404 (`UserService.java:47`,
   `ControllerExceptionHandler.java:22-25`).
3. If matched, look up the override row (`UserService.java:43`). If present → map the profile with the
   override's privileges (`UserService.java:44`).
4. Otherwise fetch the user's AAD groups (`UserService.java:57` → `GraphServiceImpl.java:43-49`), load the
   application by name, intersect its roles against the group display names, and flatten to privilege names
   (`UserService.java:59-73`).

### 3.1 Why a step gets skipped

- **Azure AD is skipped entirely** when an override row exists on `/api/authorities/...`
  (`UserService.java:37-38`). A username that does not exist in AAD still gets HTTP 200 with privileges on
  that endpoint, while the same username gets 404 on `/api/user/...`. The two endpoints legitimately
  disagree.
- **Group intersection is skipped** when the application name is not found — `findByName` returns empty and
  the result is an empty set, not an error (`UserService.java:59-62`).
- **Directory objects that are not groups are silently dropped** from membership before any matching
  happens (`GraphServiceImpl.java:45-47`).
- **Only the first page of memberships is considered**, capped at 999 (`GraphApiService.java:38-42`,
  `:47`). There is no paging loop.
- **Only the first AAD search hit is used.** `findFirst` on the first page (`GraphApiService.java:58-63`).
  An ambiguous or eventually-consistent search result is resolved silently.

## 4. The rules that stop something

**4.1 Caller not authenticated.** `/api/**` requires an authenticated request; sessions are stateless and
CSRF is disabled (`WebSecurityConfig.java:29-35`). A failure returns **HTTP 401** with response header
`WWW-Authenticate: Basic` and the error message text exactly `Not authorized`
(`WebSecurityConfig.java:43-44`). Note the header name casing as sent: `WWW-Authenticate`, value `Basic`.
This is a **fault** — wrong or missing credentials in the caller's config. Credentials are per environment:
`jamwsuser` (prod, `application-prod.yml:4`), `jamwsuser-dev` (`application-dev.yml:4`), `jamwsuser-sit`
(`application-sit.yml:4`), `jamwsuser-uat` (`application-test.yml:4` — yes, `-uat` on the `test` profile),
`user` (`application-local.yml:4`). Passwords are jasypt `ENC(...)` values decrypted with `encrypt.key`
(`application.yml:14-16`).

**4.2 User not found in Azure AD.** Returns **HTTP 404**. Message text, character-for-character:
`User not found with account name: ` followed by the username (`UserService.java:47`). This is normally a
**business** matter — the account does not exist under that samAccountName, or AAD's `$search` index has
not caught up (`ConsistencyLevel: eventual`, `GraphApiService.java:32`, `:54`). The identical string is
thrown from a second site, `GraphServiceImpl.java:31`, so the message alone does not identify which path
failed.

**4.3 No matching role.** Not a rule that "stops" anything — it returns **HTTP 200** with an empty
authorities array (`UserService.java:62`, `:67-72`). Empty-because-no-AAD-group and
empty-because-unknown-application are indistinguishable in the response.

**4.4** A handler for `NoSuchElementException` → HTTP 404 exists (`ControllerExceptionHandler.java:17-20`),
but no reachable throw site was found in main source — the only `Optional.get()` is guarded
(`GraphServiceImpl.java:29`, `:34`).

There is **no message bundle in this repo** — no `messages.properties` and no `MessageSource`. All
user-facing text is the string literals quoted above.

## 5. What users can do

There are no end-user actions in jam-ws. The only clients are other applications calling the two GET
endpoints in §3.

The repo does ship an Angular page (`app.component.html:1-2`): a header reading `Jam V2`
(`header.component.html:8`) and a "Search for users" panel (`user-search.component.html:1`) with a
`Username` input, a `Project` select hardcoded to `["oe", "spt"]` (`user-search.component.ts:22`), an
`Add new user` button and a `Search` submit button (`user-search.component.html:2-3`, `:20`).

**None of it does anything, silently.** No submit handler or click handler is bound in
`user-search.component.html`, and no component in the app injects an HTTP client — `HttpClientModule` is
registered in `main.ts:10`, `:20-23` and never used. The `basePath` values in
`jam-frontend/src/environments/*.ts` are read by nothing. Pressing `Search` producing no result is the
current state of the code, not a defect to escalate.

The page is served only if the app was built with the `build-front` Maven profile, which is what copies it
to `META-INF/resources/webjars/jam-frontend` (`jam-frontend/pom.xml` copy-resources execution;
`jam-backend/pom.xml:182-191`; `application.yml:7-9`). `WebConfig.java:16-18` forwards `/index.html` and
`/jam/**` there. Note the front end's own `basePath` says `/ui`, which no route serves.

## 6. Statuses, and who writes them

**jam-ws writes exactly 0 statuses.** There is no status column in any table
(`changelog-initial-table-creation-insert.xml:9-72`) and no status field on any entity
(`ApplicationEntity`, `RoleEntity`, `PrivilegeEntity`, `UserApplicationRoleEntity`).

More broadly, **jam-ws writes no rows at all**: `git ls-files | xargs grep '\.save('` and `'\.delete'`
return nothing. The repositories expose only reads (`ApplicationRepository.java:10`,
`UserApplicationRoleRepository.java:12`); `RoleRepository` and `PrivilegeRepository` declare no methods and
are not injected anywhere. Every table is populated from outside the service.

---

## 7. Log anatomy — read this before quoting any line

Config: `jam-backend/src/main/resources/logback-spring.xml`. Facts that affect parsing:

- Logback context name is `jam-ws` (`:3`) and `jmxConfigurator` is on (`:4`).
- `scan="true" scanPeriod="60 seconds"` (`:2`) — the config is re-read live, so levels at incident time may
  differ from the file in git.
- The root logger is `INFO` and is attached to **`FILE` only** (`:9-11`). `CONSOLE` is referenced only
  inside the `local` profile (`:13-36`). On any deployed environment there is no console stream.
- No `<pattern>` is declared anywhere in this repo. See §2.1 — no example line, field order, padding rule,
  stack-trace placement, multi-line joining rule or timezone can be quoted from this repository.

### 7.1 Level availability per environment

`com.computacenter.jam` is configured per profile, all with `additivity="false"`:

| Profile | `com.computacenter.jam` | Appenders | Cite |
|---|---|---|---|
| local | DEBUG | CONSOLE + FILE | `logback-spring.xml:33-36` |
| dev | DEBUG | FILE | `:74-76` |
| sit | DEBUG | FILE | `:54-56` |
| test | DEBUG | FILE | `:97-99` |
| prod | **INFO** | FILE | `:120-122` |

Statement counts, taken over tracked `*.java` (`git ls-files '*.java' | xargs grep -ho "log.<level>("`):

| Level | Count |
|---|---|
| TRACE | 0 |
| DEBUG | 5 |
| INFO | 0 |
| WARN | 0 |
| ERROR | 1 |

Total: **6 statements, all in `src/main/java`**; the test tree contains none.

**The prod-visibility consequence is severe.** All 5 DEBUG lines are suppressed at INFO, and the single
ERROR line sits in `GraphServiceImpl.findGroupsThatUserBelongsTo`, which no main-source caller invokes —
its only callers are `GraphServiceTest.java:42`, `:52`, `:59`. So **in prod, jam-ws emits zero log lines
from its own code.** Anything in the prod log file comes from Spring, Hibernate, Liquibase or the container.

Do not generalise the framework levels either. In **local/dev/sit** the whole `org.springframework` and
`org.hibernate` trees are configured (`:13-24`, `:39-47`, `:59-67`). In **test and prod** only
`org.springframework.web` and `org.springframework.security` are named (`:79-84`, `:102-107`) — every other
Spring package falls back to the root `INFO`. Prod also sets `org.hibernate` and `org.hibernate.SQL` to
ERROR (`:108-113`) and `liquibase.change` / `liquibase.executor` to WARN (`:114-119`).

Levels can also be changed at runtime: the actuator `loggers` endpoint is exposed (`application.yml:30`,
`:38-39`).

### 7.2 Retries

**There are none, anywhere.** No `@Retryable`, no resilience4j, no retry configuration of any kind in
tracked source. There is also no `RestTemplate` or `WebClient` and **no HTTP connect/read timeout configured
for the Azure AD call** — a hung Graph request has no deadline set by this repo. Whatever retry or timeout
the Microsoft Graph SDK applies internally is out of scope (§2.1).

Consequence for reading logs: **each of the six messages equals exactly one logical event.** Never collapse
repeated identical lines as "one failure retried" — a repeat means the caller called again.

### 7.3 Retention and file names

`logback-spring.xml:5` declares a fallback `LOG_FILE` of `<tmpdir>/jam-ws.log`, but it is never reached
because `logging.file.name` is always set:

- `application.yml:18-20` → **`d:/apps/jam-ws/jam-ws.log`**. Applies to **dev, sit, test and prod** — none
  of those profile files override it.
- `application-local.yml:9-11` → `c:/temp/logs/jam/jam.log`.
- `jam-backend/src/test/resources/application.yml:7-9` → `c:/temp/logs/jam/jam.log` (junit only).

Note the mismatch: the log **file** is `jam-ws.log` in every deployed environment but `jam.log` locally.
No `logging.logback.rollingpolicy.*` property is set anywhere, so the rolling policy and history depth are
whatever the inherited Spring Boot `file-appender.xml` defaults are — not determinable here (§2.1).

## 8. Identifiers in the logs

There is **no correlation id**. `grep -rn "MDC" ` over tracked files returns nothing — not on the HTTP path,
not anywhere. Nothing this service logs can be tied to a caller's request id.

What does appear in log text:

- **The username / samAccountName**, as supplied by the caller. Fragments to search on:
  `for user: ` (`UserController.java:27`, `:35`) and `for user with samAccountName ` (`GraphApiService.java:56`).
  This is the only key that joins a jam-ws line to a caller's own logs, and it is only a *username* — if the
  same person triggers several lookups there is nothing to separate them.
- **The application key** (`oe`, `spt`), after `within application: ` (`UserController.java:27`, `:35`).
- **The Azure AD object id (GUID)**, after `for user with id ` (`GraphApiService.java:37`, `:43`). This
  joins to Azure AD, not to any Computacenter service.
- Both DEBUG lines in `UserController` are the *only* place the caller-supplied `application` value is
  logged; the Graph-layer lines carry no application at all.

## 9. Journey stages and their log evidence

All six statements, verbatim, with level and emitting class. Placeholders preserved; note the space before
the colon in the last one.

| Stage | Level | Emitted by | Message |
|---|---|---|---|
| Authorities request received | DEBUG | `com.computacenter.jam.controller.UserController` (`:27`) | `Get authorities for user: {} within application: {}` |
| Profile request received | DEBUG | `com.computacenter.jam.controller.UserController` (`:35`) | `Get User With Authorities for user: {} within application: {}` |
| AAD user search issued | DEBUG | `com.computacenter.jam.graphapi.GraphApiService` (`:56`) | `Calling graph api for user with samAccountName {} to get all users available` |
| AAD membership call issued | DEBUG | `com.computacenter.jam.graphapi.GraphApiService` (`:37`) | `Calling graph api for user with id {} to get all roles/groups in azure AD` |
| AAD membership result | DEBUG | `com.computacenter.jam.graphapi.GraphApiService` (`:43`) | `Found groups/roles/directories for user with id {} are {}` |
| AAD user missing | ERROR | `com.computacenter.jam.service.GraphServiceImpl` (`:30`) | `User not found on azure AD for sam account name : {}` |

Two traps in that table. The last message has **` : `** — space, colon, space — before `{}`, unlike every
other line. And the membership-result line is emitted **before** the null check on the same result
(`GraphApiService.java:43` then `:44`), so its second `{}` can legitimately render `null`; the value is a
Graph collection-page object, so what lands in the file is that object's `toString()`, not a tidy list.

### 9.1 Lines that look like errors but are not

- `User not found on azure AD for sam account name : {}` at ERROR is the **only** ERROR in the codebase and
  it is **unreachable from either endpoint** (§7.1). If you see it, someone is running the test path or
  calling code that does not exist in this revision. The reachable 404 path
  (`UserService.java:47`) logs **nothing at all**.
- A user with no matching group returns HTTP 200 and an empty array and logs nothing beyond the DEBUG
  request lines — invisible in prod entirely.

### 9.2 Queue / topology reference

**None.** There is no messaging of any kind: no AMQP, Kafka, JMS, Rabbit or queue dependency in
`jam-backend/pom.xml`, and no such reference in tracked source. jam-ws is synchronous HTTP only. It is
always the *called* party over HTTP; its one outbound dependency is Microsoft Graph
(`GraphClientConfig.java:19-34`).

## 10. Evidence that is not in the log file

- **No error or dead-letter queues** — see §9.2. There are therefore no error-queue headers.
- **Database — CURRENT-STATE SNAPSHOT, no history.** Tables `role`, `privilege`, `role_privilege`,
  `application`, `application_role`, `user_application_role`
  (`changelog-initial-table-creation-insert.xml:9-72`). SQL Server; per-environment hosts in
  `application-prod.yml:17`, `application-dev.yml:13`, `application-sit.yml:12`, `application-test.yml:12`,
  database `JAM`. **None of these tables has an audit column** — no created_by, modified_by or timestamp
  anywhere in the changelog. A change to a privilege mapping leaves no trace of who made it or when.
- **Azure AD** is the live source of group membership and is never cached or copied locally (no
  `@Cacheable` / `CacheManager` in tracked source). Membership as of last week cannot be reconstructed.
- **Tomcat access log — a second log stream.** Directory `d:/apps/jam-ws` (`application.yml:21-24`),
  `c:/temp/logs/jam` locally (`application-local.yml:12-15`). Its file name and pattern come from
  `cc-standardized-web-app` and are unknown (§2.1). Since jam-ws logs nothing itself in prod, this may be
  the only per-request evidence that exists.
- **Actuator endpoints**, base path `/`, enabled-by-default false, exposing `health,info,loggers,refresh,shutdown`
  (`application.yml:25-43`). `health` shows `show-details: never` (`:37`). `info` reports the app name,
  description, build version and active profile (`:47-55`).
- **Secrets — redact before quoting.** `application-*.yml` carry jasypt `ENC(...)` values for
  `spring.security.user.password` and for `jam.azure.active-directory.tenant-id` / `client-id` /
  `client-secret` (e.g. `application-prod.yml:5`, `:20-22`), decrypted with the `encrypt.key` environment
  variable (`application.yml:14-16`). `application-local.yml:22` contains a **plaintext** database password.

## 11. Blind spots and traps

1. **No correlation id on any path** (§8). The only join key is a username string.
2. **No MDC at all**, so there is no cross-request leak risk and nothing to propagate — and equally no way
   to enrich a line. There is no `@Async`, executor or `CompletableFuture` in tracked source, so the
   async-propagation question does not arise in this revision.
3. **"Who granted this privilege?" is unanswerable.** jam-ws writes no rows (§6) and the tables have no
   audit columns (§10). Rows in `user_application_role` arrive from outside the service. Refuse the
   question rather than guessing.
4. **A rejection can return HTTP 200 and log nothing.** "User has no privileges" is the most common support
   report and, in prod, produces *no* log evidence: an empty set from an unknown application name
   (`UserService.java:62`) and an empty set from no group match (`UserService.java:67-72`) are identical in
   the response and both silent.
5. **PII in log text.** All DEBUG lines print the username or samAccountName; `GraphApiService.java:43`
   prints the whole Graph membership object, which may include display names and other directory
   attributes. Redact when quoting. The `/api/user` response carries email, names, job title and office
   location (`UserProfile.java:6-15`), but no code logs the response.
6. **Misleading wording.** `Found groups/roles/directories ... are {}` implies a list of groups; the value
   is an un-filtered Graph collection-page object logged before the null check, and before non-group
   objects are dropped at `GraphServiceImpl.java:45-47`.
7. **Silent narrowing in the AAD lookup:** first search hit only (`GraphApiService.java:63`), first
   membership page only, `top(999)` (`:41`, `:47`), non-`Group` objects discarded
   (`GraphServiceImpl.java:45-47`). All three produce a plausible wrong answer with no warning.
8. **Role matching is exact and case-sensitive** (`UserService.java:68` uses `List.contains` on group
   display names). A renamed AAD group, or a trailing space, silently removes every privilege.
9. **Two throw sites share one message string.** `User not found with account name: ` is thrown at
   `UserService.java:47` (reachable, logs nothing) and `GraphServiceImpl.java:31` (unreachable, logs ERROR).
   The 404 body cannot tell you which fired.
10. **Levels are mutable at runtime** — `scan="true"` (`logback-spring.xml:2`) plus the actuator `loggers`
    endpoint (`application.yml:38-39`). Never assume the git config was in force.
11. **Test seed data is not production seed data.** `src/test/resources/data.sql` adds a role
    `SPT-Manual-Insert` (`:15`), an override row for `test_user` (`:38`), and grants
    `Order Engine 1.5` / `OE 2.1 UAT-Team` only `admin`; production seed `initial-insert.sql:16-19` grants
    both `admin` and `view`. Never quote `data.sql` as production truth.
12. **Repo documentation contradicts the code.** `docs/man/002-running-application-inlocal.adoc:14-21` says
    three profiles (dev, test, prod); there are five plus `junit` (`logback-spring.xml:12-123`,
    `IntegrationTestSetup.java:20`). The `test` profile's Basic-auth user is `jamwsuser-uat`
    (`application-test.yml:4`).
13. **Three different expansions of "JAM" appear in the repo**: "Java Authorization Module"
    (`README.adoc:1`), "Java Authorisation Management" (`application.yml:49`) and "Java Authentification
    Manager" (`docs/index.adoc:34`). Do not present any one as the official name.
14. **`docs/index.adoc:12` claims callers authenticate the end user with an X509 certificate and extract the
    username from it.** Nothing in this repo does that; jam-ws receives the username as a path variable
    (`UserController.java:26`, `:34`). Treat it as a claim about the callers, not about jam-ws.
15. **Dead front-end config:** `basePath` (`jam-frontend/src/environments/*.ts`) is read by nothing, and its
    `/ui` value matches no route — `WebConfig.java:16-18` serves `/index.html` and `/jam/**`.
16. **Only `/api/**` is covered by the security chain declared here** (`WebSecurityConfig.java:29`), and the
    chain is disabled under the `junit` profile (`:23`). Whether actuator — including the enabled `shutdown`
    endpoint (`application.yml:42-43`) — is protected depends on `StandardizedWebSecurityConfig`, which is
    not readable (§2.1). Do not assert either way.
17. No untracked source trees were found in `jam-backend/src`; the counts in §7.1 cover all live code.

## 12. Escalation

Hand over, every time: **the username exactly as the caller sent it**, the **application key** (`oe` /
`spt`), **which endpoint** was called (`/api/authorities/...` vs `/api/user/...` — they behave differently,
§3.1), the environment, and a **timestamp window**. There is no correlation id or failure id to quote (§8).

| Symptom | Route to | Business decision or fault? |
|---|---|---|
| User gets no privileges, 200 + empty array | Identity / AD team first — verify AAD group membership matches a role `name` exactly (`UserService.java:68`) | **Business** in the normal case. Do not raise a jam-ws ticket before membership is confirmed. |
| 404 `User not found with account name: ...` | Identity / AD team — the samAccountName does not resolve in AAD (`UserService.java:47`) | **Business**, unless the account demonstrably exists, in which case suspect the eventual-consistency search (`GraphApiService.java:32`). |
| 401 `Not authorized` | The **calling** application's team — its configured Basic credentials (§4.1) | Fault, in the caller's config. |
| A privilege mapping is wrong or missing | jam-ws owner — the row must be changed outside the service (§6) | **Business** (data change), not a code fault. |
| Graph call hangs, or 5xx from jam-ws | jam-ws owner — note there is no configured timeout or retry (§7.2) | Fault. |
| "Search" in the JAM UI does nothing | jam-ws owner as a feature request | Not a fault — the handlers do not exist (§5). |

Repo owner and links (`README.adoc:12`, `:16`, `:23-24`): GitLab
`http://gitlab-csd.computacenter.com:7050/mutabilis/jam-2.0`, Jenkins
`http://jenkins-csd.computacenter.com/view/JAM/job/jam/`, contact Ermir Cjapi
(Ermir.Cjapi@computacenter.com). Deployment target group DEV is triggered from GitLab CI
(`.gitlab-ci.yml:86-99`).

## 13. Provenance and known unknowns

Verified against commit `145f679` ("Updated application master branch version to 0.10.0-SNAPSHOT
[ci skip]", 2024-07-08) on branch `master`, application version `0.10.0-SNAPSHOT` (`pom.xml:14`).
Documented on 2026-07-29. The untracked file `_TEMPLATE-new-service.md` was ignored as a source.

| Section | Status |
|---|---|
| 1, 1.1 | verified-in-code |
| 2 | verified-in-code |
| 2.1 | verified-in-code (as gaps) |
| 3, 3.1 | verified-in-code |
| 4 | verified-in-code |
| 5 | verified-in-code |
| 6 | verified-in-code (grepped for writes; none exist) |
| 7 | verified-in-code, **incomplete** — no pattern/example line obtainable |
| 7.1 | verified-in-code, counts recomputed off `git ls-files` |
| 7.2 | verified-in-code (verified absence) |
| 7.3 | verified-in-code for file names; rolling policy **unknown** |
| 8 | verified-in-code |
| 9, 9.1, 9.2 | verified-in-code |
| 10 | verified-in-code, except the access-log file name (**unknown**) |
| 11 | verified-in-code |
| 12 | verified-in-code for symptoms; owner names from `README.adoc` — **not confirmed current** |
| 13 | verified-in-code |
