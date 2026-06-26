# Restart-safe Todoist Webhook ACK

## TL;DR
> **Summary**: Convert Todoist webhook handling to ACK-first inside the existing `todoist-proxy` process: validate, durably record/enqueue in SQLite, return exact HTTP `200`, and retry downstream Hermes delivery locally. Keep the YAGNI/Ponytail path: no separate ingress service, no external broker, no systemd mutation, no broad refactor.
> **Deliverables**:
> - Minimal durable `inbound_events` + pending work schema/helpers in `control_ledger.py`
> - Public `proxy.py` webhook path that never waits on downstream forwarding before ACK
> - Local deterministic drain/worker function for pending deliveries and retryable route resolution
> - Source-grounded pytest coverage using existing stub request, fake upstream, and temp SQLite patterns
> - Optional README note only if behavior docs must be corrected
> **Effort**: Large
> **Parallel**: YES - 4 waves
> **Critical Path**: Task 1 → Task 2 → Task 3 → Task 5 → Task 6 → Final Verification

## Context

### Original Request
- User asked: “check this plan vs the source code and turn it into a full plan. Use the ponytail skill to preserve yagni principles”
- Source plan: `/home/filipp/Projects/todoist-proxy/restart-safe-ack-plan.md`

### Interview Summary
- No user interview was needed because the user explicitly requested a full plan and the source plan already contains the main design intent.
- Ponytail/YAGNI is active: implement the smallest safe source-compatible change, reuse existing helpers, and defer robust split-ingress work unless measured restart losses remain.

### Source Findings
- `proxy.py:406` defines `handle(request)`.
- `proxy.py:412-442` already performs body read, size check, HMAC validation, and JSON parse in the right trust-boundary order.
- `proxy.py:431-433` currently returns `200` for the disabled sentinel before JSON parsing and before ledger recording; this conflicts with “valid events are recorded even when forwarding is off.”
- `proxy.py:522-533` currently returns `502` for retryable `note:added` parent lookup failure; this is a valid authenticated delivery that should be ACKed after durable local persistence.
- `proxy.py:671-675` currently returns `502` when any enabled downstream target returns `>=500`; this delegates retry ownership to Todoist.
- `proxy.py:463-483` and `proxy.py:557-569` already make future-due/no-route valid events return `200` and record audit interactions; preserve the current vocabulary (`status="deferred", reason="due_in_future"`, `status="unrouted", reason="no_route"`).
- `control_ledger.py:403-489` creates the existing SQLite tables: `events`, `routing_decisions`, `interactions`, `config_audit`, `delivery_dedup`.
- `control_ledger.py:10-11` explicitly says the existing ledger does not store raw payload bodies; add a new inbound ledger rather than changing the privacy/storage contract of current audit tables.
- `control_ledger.py:133-167`, `control_ledger.py:619-717`, and `proxy.py:584-628` already implement successful per-target delivery dedup using `X-Todoist-Delivery-ID`; reuse this as the only success dedup source.
- Tests already use pytest with signed request stubs, fake upstream sessions, and temp SQLite paths in `tests/conftest.py`, `tests/test_proxy_webhook.py`, `tests/test_forwarding_controls.py`, `tests/test_ledger.py`, and `tests/test_due_poller_ledger.py`.
- Live systemd units are outside the repo at `/home/filipp/.config/systemd/user/todoist-proxy.{service,socket}`; repo docs say systemd unit files are not included.

### Metis Review (gaps addressed)
- ACK must be gated by a durable inbound/queue transaction, not best-effort audit logging.
- Public webhook handling must not synchronously forward downstream before ACK.
- Disabled mode decision: **record inbound + suppressed audit + zero pending deliveries + no replay**. This preserves `todoist-proxy off` as “off means do not forward,” not “pause and replay later.”
- Durable inbound/queue failure decision: return non-`200`, specifically `503`, so Todoist can retry instead of losing the event.
- Downstream status decision: `2xx` success, `3xx/4xx` terminal failure, `5xx`/timeout/connection error retryable.
- `note:added` parent lookup retry decision: represent as explicit routing-resolution pending work; do not create fake subscription rows before route resolution.
- Header storage decision: allowlist only Todoist-relevant headers; do not store arbitrary request headers.
- Ops decision: defer systemd unit changes and split ingress; current plan is repo-local.

## Work Objectives

### Core Objective
For every authentic Todoist delivery that reaches `proxy.py`, return exact HTTP `200` after durable local persistence, even when forwarding is disabled, no route matches, the task is future-due, route resolution needs retry, or Hermes/downstream delivery fails. Retry downstream work from SQLite instead of requiring Todoist to retry the entire webhook.

### Deliverables
- Durable inbound event table and insert helper.
- Durable pending work table and atomic enqueue helper.
- `proxy.py` ACK-first request path with no synchronous downstream POST in accepted request handling.
- Deterministic local drain/worker function for pending deliveries and retryable route resolution.
- Tests covering invalid inputs, disabled/no-route/future-due, duplicate IDs, downstream 2xx/4xx/5xx, partial fanout, raw body persistence, and retryable `note:added` lookup.
- Minimal README correction only if implementation changes documented behavior.

### Definition of Done (verifiable conditions with commands)
- `python -m pytest tests/test_ledger.py -q` passes.
- `python -m pytest tests/test_proxy_webhook.py tests/test_forwarding_controls.py tests/test_due_poller_ledger.py -q` passes.
- `python -m pytest -q` passes.
- `python -m compileall /home/filipp/Projects/todoist-proxy` passes.
- Tests prove exact HTTP `200` for accepted valid webhook cases and non-`200` for invalid/unsafe cases.
- Tests prove accepted request path does not require downstream POST completion.

### Must Have
- Exact HTTP `200` for accepted authentic Todoist deliveries.
- Exact existing invalid request behavior unless explicitly changed here: invalid HMAC `401`, malformed JSON `400`, oversized body `413`, unsupported path/method `404`.
- Durable inbound + pending transaction before returning `200` for routed/retryable work.
- `503` when the durable inbound/queue transaction fails.
- No synchronous Hermes/downstream HTTP POST in the accepted webhook request path.
- Reuse existing route matching and delivery dedup patterns.
- One deterministic drain function that pytest can call directly.
- Store exact raw body bytes only in the new inbound table.
- Store only allowlisted headers: `X-Todoist-Hmac-SHA256`, `X-Todoist-Delivery-ID`, and `Content-Type` if present.

### Must NOT Have (guardrails, AI slop patterns, scope boundaries)
- No separate always-on ingress service in this implementation.
- No external broker, job framework, message queue dependency, or daemon supervisor.
- No mutation of `/home/filipp/.config/systemd/user/todoist-proxy.service` or `.socket`.
- No replay of events suppressed while `todoist-proxy off` is active.
- No replacing `delivery_dedup`; pending work tracks owed work, `delivery_dedup` remains the success ledger.
- No broad refactor of `proxy.py`, route matching, due poller, or CLI.
- No changing current no-route/future-due semantics except adding inbound persistence.
- No storing arbitrary headers or secrets.
- No claiming true process-down ACK safety; handler changes cannot ACK while the process/socket path is unavailable.

## Behavior Matrix

| Case | HTTP response to Todoist | Inbound row | Pending work | Audit row | Downstream call in request |
|---|---:|---:|---:|---:|---:|
| invalid HMAC | `401` | no | no | no | no |
| malformed JSON | `400` | no | no | no new trusted audit | no |
| oversized body | `413` | no | no | no | no |
| durable DB failure | `503` | no/rolled back | no/rolled back | no/rolled back | no |
| disabled valid event | `200` | yes | no | suppressed/disabled | no |
| no route | `200` | yes | no | `unrouted/no_route` | no |
| future due | `200` | yes | no | `deferred/due_in_future` | no |
| routed event | `200` | yes | delivery rows | queued/accepted | no |
| retryable note parent lookup failure | `200` | yes | routing-resolution row | queued/retryable route resolution | no downstream POST |
| downstream 2xx in worker | n/a | already yes | succeeded/done | forwarded | yes |
| downstream 3xx/4xx in worker | n/a | already yes | terminal failed | failed terminal | yes |
| downstream 5xx/timeout/connection in worker | n/a | already yes | retryable | failed retryable | yes |
| duplicate delivery ID | `200` | canonical only | no duplicate work | optional duplicate audit only | no duplicate side effect |

## SQLite Schema Decisions

Add schema in `control_ledger.py` beside existing table creation in `control_ledger.py:403-489`.

### `inbound_events`
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `source TEXT NOT NULL DEFAULT 'proxy'`
- `event_name TEXT NOT NULL`
- `entity_id TEXT`
- `project_id TEXT`
- `delivery_id TEXT`
- `payload_hash TEXT NOT NULL`
- `raw_body BLOB NOT NULL`
- `headers_json TEXT NOT NULL`
- `received_at TEXT NOT NULL`
- `status TEXT NOT NULL DEFAULT 'accepted'`

Indexes:
- Partial unique index for nonempty delivery IDs: `(source, delivery_id)` where `delivery_id IS NOT NULL AND delivery_id != ''`.
- Fallback unique index for missing delivery IDs: `(source, event_name, entity_id, project_id, payload_hash)` where `delivery_id IS NULL OR delivery_id = ''`.

### `pending_deliveries`
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `inbound_event_id INTEGER NOT NULL REFERENCES inbound_events(id)`
- `kind TEXT NOT NULL` with values exactly `delivery` or `routing_resolution`
- `subscription TEXT` nullable only when `kind='routing_resolution'`
- `state TEXT NOT NULL DEFAULT 'pending'` with values exactly `pending`, `retry`, `succeeded`, `terminal_failed`, `suppressed`
- `attempt_count INTEGER NOT NULL DEFAULT 0`
- `next_attempt_at TEXT NOT NULL`
- `last_error TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Indexes:
- Unique `(inbound_event_id, kind, subscription)` for `subscription IS NOT NULL`.
- Unique `(inbound_event_id, kind)` for `kind='routing_resolution'` and `subscription IS NULL`.
- Query index on `(state, next_attempt_at)`.

YAGNI note: do not add a generic job payload column unless tests show it is necessary. The raw body lives in `inbound_events`; routing/delivery state is derived from that and existing routing config.

## Verification Strategy
> ZERO HUMAN INTERVENTION - all verification is agent-executed.
- Test decision: TDD using existing pytest files and fixtures; add failing tests before implementation for each behavior group.
- QA policy: Every task has agent-executed scenarios.
- Evidence: `.sisyphus/evidence/task-{N}-{slug}.txt`

## Execution Strategy

### Parallel Execution Waves
> Target: 5-8 tasks per wave. This plan is intentionally dependency-heavy because ACK semantics must be correct before worker behavior is meaningful.

Wave 1: Task 1 (ledger schema/helpers), Task 8 (README behavior audit; read-only until docs change is necessary)
Wave 2: Task 2 (pending queue helpers), Task 4 (suppression/no-route/future-due tests)
Wave 3: Task 3 (ACK-first public path), Task 5 (worker/drain), Task 6 (note routing resolution)
Wave 4: Task 7 (minimal CLI/status visibility), Task 9 (full regression/docs correction)

### Dependency Matrix (full, all tasks)
- Task 1: no dependencies.
- Task 2: blocked by Task 1.
- Task 3: blocked by Tasks 1 and 2.
- Task 4: blocked by Tasks 1 and 2; can run alongside Task 3 once helpers exist.
- Task 5: blocked by Tasks 2 and 3.
- Task 6: blocked by Tasks 2, 3, and 5.
- Task 7: blocked by Task 2; can run after queue schema exists.
- Task 8: no dependencies; do not edit docs until Task 9 confirms behavior changes.
- Task 9: blocked by Tasks 1-8.

### Agent Dispatch Summary (wave → task count → categories)
- Wave 1 → 2 tasks → `quick`, `writing`
- Wave 2 → 2 tasks → `unspecified-high`, `quick`
- Wave 3 → 3 tasks → `unspecified-high`, `deep`
- Wave 4 → 2 tasks → `quick`, `unspecified-high`

## TODOs
> Implementation + Test = ONE task. Never separate.
> EVERY task MUST have: Agent Profile + Parallelization + QA Scenarios.

- [x] 1. Add durable inbound event ledger

  **What to do**:
  - In `control_ledger.py`, add `inbound_events` table creation following the existing migration style at `control_ledger.py:403-489`.
  - Add helper(s) that insert or return the canonical inbound event row using `X-Todoist-Delivery-ID` when present and fallback identity when absent.
  - Store exact raw body bytes in `raw_body`.
  - Store only allowlisted headers (`X-Todoist-Hmac-SHA256`, `X-Todoist-Delivery-ID`, `Content-Type`) as JSON.
  - Keep current `events`/`interactions` schema unchanged.
  - Add TDD tests in `tests/test_ledger.py` using the existing temp SQLite pattern.

  **Must NOT do**:
  - Do not retrofit raw payload storage into existing `events` or `interactions`.
  - Do not store arbitrary request headers.
  - Do not add a generic ORM or migration framework.

  **Recommended Agent Profile**:
  - Category: `quick` - focused schema/helper addition in one module plus tests.
  - Skills: [`ponytail`] - enforce smallest schema that passes required behavior.
  - Omitted: [`supabase-postgres-best-practices`] - local SQLite only, not Postgres.

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: [2, 3, 4, 5, 6, 7, 9] | Blocked By: []

  **References**:
  - Pattern: `control_ledger.py:403-489` - existing SQLite schema creation style.
  - Pattern: `control_ledger.py:495-520` - existing event-recording helper style.
  - Pattern: `control_ledger.py:133-167` - existing delivery identity extraction and delivery ID preference.
  - Test: `tests/test_ledger.py:22-39` - schema/WAL/busy timeout test style.
  - Test: `tests/test_ledger.py:213-254` - delivery ID preference and payload-hash fallback tests.

  **Acceptance Criteria**:
  - [ ] `python -m pytest tests/test_ledger.py -q` passes.
  - [ ] Test proves duplicate nonempty `X-Todoist-Delivery-ID` creates/returns one canonical inbound row.
  - [ ] Test proves missing-delivery-ID exact duplicate is idempotent by fallback identity.
  - [ ] Test proves two missing-delivery-ID events with different body/entity are recorded independently.
  - [ ] Test proves stored `raw_body` bytes exactly equal the signed bytes, including whitespace/key order.
  - [ ] Test proves stored `headers_json` contains only allowlisted headers.

  **QA Scenarios**:
  ```
  Scenario: Duplicate delivery ID canonicalizes inbound event
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_ledger.py -q
    Expected: ledger test asserts same delivery ID returns one canonical inbound row and exits 0
    Evidence: .sisyphus/evidence/task-1-inbound-ledger.txt

  Scenario: Missing delivery ID fallback does not collapse distinct events
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_ledger.py -q
    Expected: ledger test asserts distinct raw bodies/entities without delivery ID create distinct inbound rows and exits 0
    Evidence: .sisyphus/evidence/task-1-inbound-ledger-fallback.txt
  ```

  **Commit**: NO | Message: `Add durable inbound event ledger` | Files: [`control_ledger.py`, `tests/test_ledger.py`]

- [x] 2. Add atomic pending work queue helpers

  **What to do**:
  - In `control_ledger.py`, add `pending_deliveries` table and indexes exactly as defined in this plan.
  - Add helper(s) for a single transaction that records inbound event and creates pending work.
  - Support `kind='delivery'` with known `subscription`.
  - Support `kind='routing_resolution'` with `subscription IS NULL` for retryable `note:added` parent lookup failures.
  - Add helper(s) for queue depth and due-work selection by `state IN ('pending', 'retry') AND next_attempt_at <= now`.
  - On transaction failure, leave no partial pending rows.
  - Add tests in `tests/test_ledger.py`.

  **Must NOT do**:
  - Do not create fake subscription rows before route resolution is known.
  - Do not treat `pending_deliveries` as success dedup; success remains `delivery_dedup`.
  - Do not build a generic job framework.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - queue semantics and transactions require care.
  - Skills: [`ponytail`] - keep queue state minimal.
  - Omitted: [`database-migrations`] - no heavy migration framework; existing SQLite style is enough.

  **Parallelization**: Can Parallel: NO | Wave 2 | Blocks: [3, 4, 5, 6, 7, 9] | Blocked By: [1]

  **References**:
  - Pattern: `control_ledger.py:403-489` - schema creation style.
  - Pattern: `control_ledger.py:619-717` - existing delivery-dedup success helpers to keep separate.
  - Test: `tests/test_ledger.py:257-287` - SQLite write failure/nonfatal behavior; new durable queue failure must be fatal to ACK path but test style is useful.

  **Acceptance Criteria**:
  - [ ] `python -m pytest tests/test_ledger.py -q` passes.
  - [ ] Test proves inbound + pending delivery rows are created atomically.
  - [ ] Test proves simulated pending insert failure rolls back inbound insert in the atomic helper used by the ACK path.
  - [ ] Test proves `routing_resolution` work can exist without `subscription`.
  - [ ] Test proves duplicate `(inbound_event_id, kind, subscription)` does not create duplicate delivery work.
  - [ ] Test proves queue depth helper returns pending/retry count only.

  **QA Scenarios**:
  ```
  Scenario: Atomic enqueue succeeds
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_ledger.py -q
    Expected: tests assert one inbound row and expected pending rows are created in one transaction
    Evidence: .sisyphus/evidence/task-2-pending-queue.txt

  Scenario: Atomic enqueue failure rolls back
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_ledger.py -q
    Expected: monkeypatched failing insert test asserts no partial inbound/pending rows remain and exits 0
    Evidence: .sisyphus/evidence/task-2-pending-queue-rollback.txt
  ```

  **Commit**: NO | Message: `Add atomic pending delivery queue` | Files: [`control_ledger.py`, `tests/test_ledger.py`]

- [x] 3. Convert public webhook path to durable ACK-first

  **What to do**:
  - In `proxy.py`, preserve the existing trust-boundary order from `proxy.py:412-442`: method/path/body/size/HMAC/JSON.
  - After valid HMAC + JSON, record inbound event using Task 1 helper before any disabled/routing/downstream work.
  - For routed work, create pending delivery rows using Task 2 helper and return exact HTTP `200` without calling downstream POST in the request path.
  - If the durable inbound/queue transaction fails, return `503` and do not call downstream.
  - Remove/replace current synchronous downstream response-driven `502` path at `proxy.py:671-675` for public accepted requests.
  - Preserve invalid HMAC `401`, malformed JSON `400`, body too large `413`, and non-webhook `404` behavior.
  - Add/adjust tests in `tests/test_proxy_webhook.py` and `tests/test_forwarding_controls.py`.

  **Must NOT do**:
  - Do not await downstream Hermes forwarding before responding to Todoist.
  - Do not return `202`; accepted Todoist delivery must receive exact `200`.
  - Do not move HMAC validation after JSON parsing.
  - Do not swallow durable DB failures with `200`.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - core request semantics and reliability boundary.
  - Skills: [`ponytail`] - avoid broad refactor; change only the ACK boundary.
  - Omitted: [`systematic-debugging`] - this is planned behavior change, not unknown bug investigation.

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: [5, 6, 9] | Blocked By: [1, 2]

  **References**:
  - Pattern: `proxy.py:406` - webhook handler entrypoint.
  - Pattern: `proxy.py:412-442` - validation order to preserve.
  - Conflict: `proxy.py:671-675` - current downstream `>=500` returns `502`; replace with local retry semantics.
  - Test: `tests/test_proxy_webhook.py:24-42` - `StubRequest`.
  - Test: `tests/test_proxy_webhook.py:102-123` - signed request builder.
  - Test: `tests/test_forwarding_controls.py:105-146` - current `502` downstream behavior tests to update.

  **Acceptance Criteria**:
  - [ ] `python -m pytest tests/test_proxy_webhook.py tests/test_forwarding_controls.py -q` passes.
  - [ ] Invalid signature test returns `401` and creates no inbound/pending rows.
  - [ ] Malformed JSON with valid signature returns `400` and creates no inbound/pending rows.
  - [ ] Durable enqueue failure returns `503` and creates no partial pending rows.
  - [ ] Routed valid event returns exact `200` and creates inbound + pending rows.
  - [ ] Routed valid event does not call fake downstream POST before returning.

  **QA Scenarios**:
  ```
  Scenario: Routed event ACKs before downstream delivery
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_proxy_webhook.py tests/test_forwarding_controls.py -q
    Expected: tests assert valid routed request returns exact 200, creates pending work, and fake downstream POST count is 0 during request handling
    Evidence: .sisyphus/evidence/task-3-ack-first.txt

  Scenario: Durable enqueue failure does not ACK
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_proxy_webhook.py -q
    Expected: monkeypatched durable transaction failure returns 503 and leaves no partial queue state
    Evidence: .sisyphus/evidence/task-3-enqueue-failure.txt
  ```

  **Commit**: NO | Message: `ACK Todoist webhooks after durable enqueue` | Files: [`proxy.py`, `tests/test_proxy_webhook.py`, `tests/test_forwarding_controls.py`]

- [x] 4. Preserve disabled, no-route, and future-due accepted semantics

  **What to do**:
  - In `proxy.py`, replace the early disabled sentinel return at `proxy.py:431-433`.
  - Disabled valid event behavior: HMAC validate, JSON parse, record inbound, record suppressed/disabled audit using existing ledger/gate patterns, create zero pending rows, perform zero route-resolution Todoist GETs, perform zero downstream POSTs, return exact `200`.
  - Keep `todoist-proxy off` semantics as “do not forward and do not replay later.”
  - Preserve existing no-route behavior at `proxy.py:557-569`: inbound row + `unrouted/no_route` audit + zero pending + exact `200`.
  - Preserve existing future-due behavior at `proxy.py:463-483`: inbound row + `deferred/due_in_future` audit + zero pending + exact `200`.
  - Add/adjust tests in `tests/test_proxy_webhook.py` and `tests/test_control_config.py` only if helper behavior changes.

  **Must NOT do**:
  - Do not replay events received while disabled.
  - Do not call Todoist parent lookup while disabled.
  - Do not rename existing `deferred/due_in_future` or `unrouted/no_route` statuses.

  **Recommended Agent Profile**:
  - Category: `quick` - narrow edge-path correction once Tasks 1-2 exist.
  - Skills: [`ponytail`] - use existing sentinel/evaluate_forwarding behavior rather than adding new control abstractions.
  - Omitted: [`backend-patterns`] - unnecessary for simple handler edge states.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: [9] | Blocked By: [1, 2]

  **References**:
  - Conflict: `proxy.py:431-433` - early disabled return to replace.
  - Pattern: `control_ledger.py:219-249` - existing sentinel-aware forwarding gate helper.
  - Pattern: `proxy.py:463-483` - existing future-due accepted behavior.
  - Pattern: `proxy.py:557-569` - existing no-route accepted behavior.
  - Test: `tests/test_control_config.py:79-98` - sentinel gate helper tests.
  - Test: `tests/test_proxy_webhook.py:640-688` - future-due/no-route tests.

  **Acceptance Criteria**:
  - [ ] `python -m pytest tests/test_proxy_webhook.py tests/test_control_config.py -q` passes.
  - [ ] Disabled valid event returns exact `200`, records one inbound row, records suppressed audit, creates zero pending rows.
  - [ ] Disabled `note:added` missing direct `project_id` performs zero fake Todoist GET calls.
  - [ ] No-route valid event returns exact `200`, records inbound row, preserves `unrouted/no_route`, creates zero pending rows.
  - [ ] Future-due valid event returns exact `200`, records inbound row, preserves `deferred/due_in_future`, creates zero pending rows.

  **QA Scenarios**:
  ```
  Scenario: Disabled mode records and suppresses without forwarding
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_proxy_webhook.py tests/test_control_config.py -q
    Expected: disabled valid event test asserts exact 200, inbound row exists, suppressed audit exists, pending count is 0, fake downstream POST count is 0
    Evidence: .sisyphus/evidence/task-4-disabled-suppressed.txt

  Scenario: Disabled note event avoids parent lookup
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_proxy_webhook.py -q
    Expected: disabled note:added test asserts fake session GET count is 0 and exits 0
    Evidence: .sisyphus/evidence/task-4-disabled-note-no-lookup.txt
  ```

  **Commit**: NO | Message: `Preserve suppression paths under durable ingress` | Files: [`proxy.py`, `tests/test_proxy_webhook.py`, `tests/test_control_config.py`]

- [x] 5. Add local pending delivery drain and retry semantics

  **What to do**:
  - Add a deterministic drain function that tests can call directly. Prefer placing it near existing forwarding code in `proxy.py` or a tiny module only if avoiding circular imports requires it.
  - Drain due `pending_deliveries` where `kind='delivery'` and `state IN ('pending', 'retry')`.
  - Reconstruct payload from `inbound_events.raw_body`.
  - Reuse existing forwarding behavior from `proxy.py:270-293` and recording behavior from `proxy.py:363-403`.
  - Reuse existing `has_successful_delivery` / `record_successful_delivery` from `control_ledger.py:619-717`.
  - Status rules: `2xx` → mark succeeded + record `delivery_dedup`; `3xx/4xx` → terminal_failed, no dedup, no automatic retry; `5xx`/timeout/connection exception → retry with incremented attempt count and `next_attempt_at` backoff.
  - Partial fanout rule: if two targets succeeded and one failed, only the failed target remains retryable.
  - Add/adjust tests in `tests/test_forwarding_controls.py` and, if due-poller helper reuse occurs, `tests/test_due_poller_ledger.py`.

  **Must NOT do**:
  - Do not add a long-running daemon, scheduler, or systemd timer in this task.
  - Do not re-forward targets that already have `delivery_dedup` success rows.
  - Do not retry 3xx/4xx forever.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - retry and partial fanout semantics are correctness-sensitive.
  - Skills: [`ponytail`] - callable drain first, no daemon until proven needed.
  - Omitted: [`cicd-automation:deployment-pipeline-design`] - no deployment pipeline changes.

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: [6, 9] | Blocked By: [2, 3]

  **References**:
  - Pattern: `proxy.py:270-293` - existing downstream POST helper.
  - Pattern: `proxy.py:363-403` - existing forward interaction recording.
  - Pattern: `proxy.py:584-628` - existing per-target successful-delivery skip logic.
  - Pattern: `control_ledger.py:619-717` - existing successful delivery dedup helpers.
  - Pattern: `due_poller.py:541-545` - existing retry-by-not-marking-fired model.
  - Test: `tests/test_forwarding_controls.py:171-256` - delivery dedup and partial retry semantics.
  - Test: `tests/test_due_poller_ledger.py:361-416` - fake-delivery partial retry tests.

  **Acceptance Criteria**:
  - [ ] `python -m pytest tests/test_forwarding_controls.py tests/test_due_poller_ledger.py -q` passes.
  - [ ] Worker/drain `2xx` test marks pending succeeded and records `delivery_dedup`.
  - [ ] Worker/drain `5xx` test marks pending retryable, increments `attempt_count`, sets `next_attempt_at`, and does not record dedup.
  - [ ] Worker/drain `4xx` test marks terminal failure, does not record dedup, and does not retry on next drain.
  - [ ] Partial fanout test proves next drain sends only failed targets.
  - [ ] Duplicate delivery ID test proves no duplicate downstream side effect.

  **QA Scenarios**:
  ```
  Scenario: Downstream 5xx becomes local retry
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_forwarding_controls.py -q
    Expected: worker test asserts pending state becomes retry, attempt_count increments, next_attempt_at updates, and delivery_dedup has no success row
    Evidence: .sisyphus/evidence/task-5-worker-retry.txt

  Scenario: Partial fanout retries only failed target
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_forwarding_controls.py tests/test_due_poller_ledger.py -q
    Expected: tests assert succeeded targets have dedup rows and next drain calls only failed subscription
    Evidence: .sisyphus/evidence/task-5-partial-fanout.txt
  ```

  **Commit**: NO | Message: `Retry pending webhook deliveries locally` | Files: [`proxy.py`, `control_ledger.py`, `tests/test_forwarding_controls.py`, `tests/test_due_poller_ledger.py`]

- [x] 6. Queue retryable `note:added` route resolution

  **What to do**:
  - Replace current retryable note parent lookup `502` behavior at `proxy.py:522-533` with inbound persistence + `routing_resolution` pending work + exact HTTP `200`.
  - Do not create subscription delivery rows until route resolution succeeds.
  - Extend the drain function from Task 5 to process `kind='routing_resolution'` rows.
  - On route resolution success, reuse `route_matcher.py` and existing routing logic to create delivery pending rows or immediately drain them using the same delivery path.
  - On route resolution retryable failure, keep/increment retry state and backoff.
  - On route resolution terminal failure (invalid/missing parent not recoverable), mark terminal and record audit reason.
  - Add/adjust tests in `tests/test_proxy_webhook.py` and `tests/test_forwarding_controls.py`.

  **Must NOT do**:
  - Do not fake a subscription value before parent task/project/route is known.
  - Do not duplicate route matching logic outside existing route helpers more than necessary.
  - Do not perform parent lookup while disabled; Task 4 already defines disabled suppression.

  **Recommended Agent Profile**:
  - Category: `deep` - route-resolution retry spans handler, queue, and worker.
  - Skills: [`ponytail`] - implement only the `note:added` unresolved-route state, not a general rules engine.
  - Omitted: [`backend-development:workflow-orchestration-patterns`] - Temporal/workflow engines are overkill.

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: [9] | Blocked By: [2, 3, 5]

  **References**:
  - Conflict: `proxy.py:522-533` - current retryable parent lookup returns `502`.
  - Pattern: `proxy.py:489-544` - existing two-pass `note:added` route context logic.
  - Pattern: `route_matcher.py:120-247` - existing route matcher to reuse.
  - Test: `tests/test_proxy_webhook.py` - note-related signed request/fake GET patterns.

  **Acceptance Criteria**:
  - [ ] `python -m pytest tests/test_proxy_webhook.py tests/test_forwarding_controls.py -q` passes.
  - [ ] Valid `note:added` parent lookup `503` returns exact `200`, records inbound row, creates `routing_resolution` pending work, and creates no fake subscription delivery row.
  - [ ] Drain retry of routing resolution succeeds when fake parent lookup later returns project context.
  - [ ] Successful routing resolution creates/sends matched delivery work using existing delivery drain semantics.
  - [ ] Retryable routing resolution failure increments attempt/backoff.

  **QA Scenarios**:
  ```
  Scenario: Retryable note parent lookup queues routing-resolution work
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_proxy_webhook.py -q
    Expected: test asserts note:added parent lookup 503 returns exact 200, one inbound row exists, routing_resolution pending row exists, no subscription delivery row exists
    Evidence: .sisyphus/evidence/task-6-note-routing-resolution.txt

  Scenario: Resolved note routing later sends matched deliveries
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_forwarding_controls.py -q
    Expected: drain test asserts later parent lookup success resolves route and delivery success records existing delivery_dedup
    Evidence: .sisyphus/evidence/task-6-note-routing-drain.txt
  ```

  **Commit**: NO | Message: `Queue retryable note route resolution` | Files: [`proxy.py`, `control_ledger.py`, `tests/test_proxy_webhook.py`, `tests/test_forwarding_controls.py`]

- [x] 7. Add minimal queue visibility to existing CLI/status

  **What to do**:
  - Extend `/home/filipp/Projects/todoist-proxy/todoist-proxy:27-38` status output to include pending queue depth from the new helper.
  - If reading the queue depth fails, print a clear warning but keep existing service/sentinel status output.
  - Optionally include socket active state via `systemctl --user is-active todoist-proxy.socket` because live socket activation exists; do not require socket unit mutation.
  - Add or update a focused CLI test only if existing `tests/test_cli_launcher.py` pattern can cover it without real systemd.

  **Must NOT do**:
  - Do not add public signed canary in this task.
  - Do not edit live systemd unit files.
  - Do not build dashboard/admin UI.

  **Recommended Agent Profile**:
  - Category: `quick` - small CLI visibility change.
  - Skills: [`ponytail`] - status only, no canary/dashboard.
  - Omitted: [`playwright-cli`] - no browser interaction.

  **Parallelization**: Can Parallel: YES | Wave 4 | Blocks: [9] | Blocked By: [2]

  **References**:
  - Pattern: `todoist-proxy:27-38` - current status output.
  - Pattern: `todoist-proxy:81-99` - existing on/off/restart/log operational style.
  - Test: `tests/test_cli_launcher.py:11-37` - CLI script testing style.

  **Acceptance Criteria**:
  - [ ] `python -m pytest tests/test_cli_launcher.py -q` passes if CLI tests are touched.
  - [ ] `todoist-proxy status` still reports service active/inactive and enabled/disabled sentinel state.
  - [ ] `todoist-proxy status` reports pending queue depth when the database exists.
  - [ ] Queue-depth read failure does not hide service/sentinel status.

  **QA Scenarios**:
  ```
  Scenario: Status includes pending queue depth
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_cli_launcher.py -q
    Expected: CLI/status test or script-level smoke asserts pending queue depth appears when helper returns a count
    Evidence: .sisyphus/evidence/task-7-status-depth.txt

  Scenario: Status remains useful when queue read fails
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_cli_launcher.py -q
    Expected: CLI test asserts status still shows service/sentinel status and a queue warning when DB read fails
    Evidence: .sisyphus/evidence/task-7-status-failure.txt
  ```

  **Commit**: NO | Message: `Show pending webhook queue depth in status` | Files: [`todoist-proxy`, `tests/test_cli_launcher.py`]

- [x] 8. Audit README behavior docs and defer non-YAGNI ops work

  **What to do**:
  - Review README sections around disabled forwarding and verification commands, especially `README.md:58-73`, `README.md:302-303`, and `README.md:340-351`.
  - Do not edit docs yet unless implementation in Tasks 1-7 changes documented user-facing behavior or corrects an existing false claim.
  - If docs are edited, state the exact new semantics: disabled mode records inbound + suppressed audit + no replay; downstream failures are retried locally after ACK; true process-down ACK safety remains out of scope.
  - Do not add systemd templates or live unit edits.

  **Must NOT do**:
  - Do not turn README into an architecture essay.
  - Do not document future split ingress as implemented.
  - Do not add canary instructions unless a canary command is actually implemented.

  **Recommended Agent Profile**:
  - Category: `writing` - docs audit/correction only.
  - Skills: [`ponytail`] - minimal docs correction, not speculative architecture.
  - Omitted: [`stop-slop`] - README should match project voice; no marketing prose.

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: [9] | Blocked By: []

  **References**:
  - Pattern: `README.md:58-73` - CLI/systemd docs.
  - Conflict: `README.md:302-303` - says disabled mode records event/decision; currently false, should become true after Task 4.
  - Pattern: `README.md:340-351` - verification commands to keep/update.
  - Source: `restart-safe-ack-plan.md:125-133` - split ingress is optional/deferred, not current implementation.

  **Acceptance Criteria**:
  - [ ] If README is unchanged, executor records why no doc edit was necessary in evidence.
  - [ ] If README is changed, it mentions only implemented behavior and deferred limitations.
  - [ ] No docs claim separate ingress/systemd changes were implemented.

  **QA Scenarios**:
  ```
  Scenario: README does not overclaim restart safety
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest -q
    Expected: docs audit evidence states README either unchanged or updated to clarify in-process ACK-first limitation; tests still pass
    Evidence: .sisyphus/evidence/task-8-readme-audit.txt

  Scenario: Disabled-mode docs match implementation
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest tests/test_proxy_webhook.py -q
    Expected: docs audit cites disabled-mode test proving README claim if README says disabled events are recorded
    Evidence: .sisyphus/evidence/task-8-disabled-docs.txt
  ```

  **Commit**: NO | Message: `Document restart-safe ACK behavior` | Files: [`README.md`]

- [x] 9. Full regression, compile check, and implementation cleanup

  **What to do**:
  - Run targeted tests for ledger, proxy webhook, forwarding controls, due poller, CLI if touched.
  - Run full pytest suite.
  - Run compile check.
  - Review diff for YAGNI violations: new abstractions with one implementation, broad unrelated refactors, added dependencies, systemd mutation, replay-while-disabled, or raw arbitrary header storage.
  - If failures are unrelated, document evidence and keep them separate; otherwise fix before final verification.

  **Must NOT do**:
  - Do not format/refactor unrelated files.
  - Do not commit unless explicitly requested by user.
  - Do not mark final verification tasks complete without user approval after review agents report.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - integration verification across all touched behavior.
  - Skills: [`ponytail`] - diff cleanup must remove overbuilt work.
  - Omitted: [`git-master`] - no git commit requested.

  **Parallelization**: Can Parallel: NO | Wave 4 | Blocks: [Final Verification] | Blocked By: [1, 2, 3, 4, 5, 6, 7, 8]

  **References**:
  - Command: `python -m pytest tests/test_ledger.py -q`
  - Command: `python -m pytest tests/test_proxy_webhook.py tests/test_forwarding_controls.py tests/test_due_poller_ledger.py -q`
  - Command: `python -m pytest -q`
  - Command: `python -m compileall /home/filipp/Projects/todoist-proxy`
  - Evidence pattern: `.sisyphus/evidence/task-7-full-pytest.txt` - prior evidence format.

  **Acceptance Criteria**:
  - [ ] Targeted tests pass.
  - [ ] Full pytest passes.
  - [ ] Compile check passes.
  - [ ] Diff contains no new dependency, no systemd mutation, no external queue, no separate ingress service, no replay-while-disabled.
  - [ ] Evidence files exist for all major test commands.

  **QA Scenarios**:
  ```
  Scenario: Full regression suite passes
    Tool: Bash
    Steps: cd /home/filipp/Projects/todoist-proxy && python -m pytest -q
    Expected: pytest exits 0
    Evidence: .sisyphus/evidence/task-9-full-pytest.txt

  Scenario: Python compile check passes
    Tool: Bash
    Steps: python -m compileall /home/filipp/Projects/todoist-proxy
    Expected: compileall exits 0
    Evidence: .sisyphus/evidence/task-9-compileall.txt
  ```

  **Commit**: NO | Message: `Verify restart-safe ACK implementation` | Files: [all touched files]

## Final Verification Wave (MANDATORY — after ALL implementation tasks)
> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback -> fix -> re-run -> present again -> wait for okay.
- [x] F1. Plan Compliance Audit — oracle
  - Verify exact HTTP behavior, durable ACK boundary, disabled/no-route/future-due semantics, and deferred non-goals against this plan.
- [x] F2. Code Quality Review — unspecified-high
  - Verify minimal diff, no broad refactor, no needless abstractions, no duplicate routing/dedup logic.
- [x] F3. Real Manual QA — unspecified-high
  - Run agent-executed synthetic signed webhook checks against local handler/test harness; if a dev server is used, capture request/response evidence.
- [x] F4. Scope Fidelity Check — deep
  - Verify no systemd mutation, no separate ingress service, no external broker, no replay-while-disabled, no arbitrary header storage.

## Commit Strategy
- Do not commit unless the user explicitly asks.
- If commits are later requested, use the task commit messages listed above and stage only intended files.
- Before any commit: inspect `git status`, `git diff`, and `git log --oneline -10`.

## Success Criteria
- Every authentic Todoist delivery that reaches `proxy.py` receives exact HTTP `200` after durable local persistence, except when durable persistence fails.
- Invalid HMAC/malformed/oversized requests are not trusted and are not inserted into inbound/pending work.
- Downstream Hermes failures no longer cause Todoist-facing `502` for accepted deliveries.
- Disabled mode records inbound/suppressed audit and does not forward or replay.
- No-route and future-due behavior remains accepted and non-forwarding.
- `note:added` parent lookup retryable failures are queued locally for route resolution instead of returning `502`.
- Existing `delivery_dedup` remains the success dedup source.
- Full tests and compile checks pass.
- The implementation remains Ponytail/YAGNI-compliant: smallest local SQLite/process change; no separate ingress or external queue until measured need.
