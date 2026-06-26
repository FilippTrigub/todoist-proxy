
## 2026-06-26 — External webhook ACK/durability docs

- Todoist official webhook docs: webhook callback URLs must be HTTPS and include no explicit port; webhook requests may be delayed, out of order, or fail to arrive, so webhooks are notifications, not a primary data source. Source: https://developer.todoist.com/sync/v8/#tag/Webhooks
- Todoist official webhook request docs: request headers include `User-Agent: Todoist-Webhooks`, `X-Todoist-Hmac-SHA256`, and `X-Todoist-Delivery-ID`. HMAC is SHA256 using the app `client_secret` as key and the whole raw request payload as the message, base64 encoded. Source: https://developer.todoist.com/sync/v8/#tag/Webhooks/Request-Format
- Todoist official retry/ACK docs: `X-Todoist-Delivery-ID` is unique per event notification and failed redeliveries use the same delivery ID. Failed deliveries from server/network error or incorrect response are retried after 15 minutes, at most three times. The callback endpoint must respond HTTP 200; any non-200 is considered failed and retried. Source: https://developer.todoist.com/sync/v8/#tag/Webhooks/Request-Format
- aiohttp official docs/source: web handlers return `StreamResponse`/`Response` instances; `web.Response` defaults `status=200`. Evidence: docs source https://github.com/aio-libs/aiohttp/blob/7987bd2ccfc6fc48adca7e97ab57abebbd67179e/docs/web_quickstart.rst#L104-L108 and response source https://github.com/aio-libs/aiohttp/blob/7987bd2ccfc6fc48adca7e97ab57abebbd67179e/aiohttp/web_response.py#L539-L551
- Python sqlite3 official docs: INSERT opens a transaction that must be committed before changes are saved; `Connection.commit()` commits pending transactions; closing can lose pending changes unless committed. A `Connection` context manager commits on clean exit and rolls back on uncaught exception. Evidence: https://github.com/python/cpython/blob/4fa86ca03da8c2068202d8ac3ddcf14640e2f2b9/Doc/library/sqlite3.rst#L152-L160, https://github.com/python/cpython/blob/4fa86ca03da8c2068202d8ac3ddcf14640e2f2b9/Doc/library/sqlite3.rst#L654-L678, https://github.com/python/cpython/blob/4fa86ca03da8c2068202d8ac3ddcf14640e2f2b9/Doc/library/sqlite3.rst#L2400-L2420
- SQLite official docs: `INSERT OR IGNORE` is SQLite’s documented conflict-resolution syntax for skipping rows that violate applicable UNIQUE/NOT NULL constraints without returning an error; this supports idempotent insert when paired with a UNIQUE key such as delivery ID. Source: https://www.sqlite.org/lang_conflict.html
- Research note: official Todoist docs found exact retry delay/count and exact success status requirement, but did not find a shorter ACK deadline in the official docs. Treat non-official claims about timeouts as unverified unless Todoist documents them elsewhere.

## 2026-06-26 — Task 1 inbound ledger implementation

- `ControlLedger.record_inbound_event()` now mirrors the existing delivery-dedup pattern: `INSERT OR IGNORE`, then fetch the canonical row by nonempty `(source, delivery_id)` or missing-delivery fallback `(source, event_name, entity_id, project_id, raw payload hash)`.
- The new `inbound_events` table is the only ledger table that stores exact raw request bytes; existing `events` and `interactions` still store normalized fields plus hashes only.
- Header persistence is intentionally allowlisted to `X-Todoist-Hmac-SHA256`, `X-Todoist-Delivery-ID`, and `Content-Type`, with canonical header names in stored JSON.


## 2026-06-26 — Task 8 README audit

- README docs needed a small correction after Task 1 because `inbound_events.raw_body` now stores exact authenticated raw webhook bytes; older audit tables remain hash/metadata-only.
- Disabled proxy docs must not claim inbound + suppressed audit recording yet. After Task 1 only, README should say disabled proxy forwarding validates HMAC and suppresses delivery, with disabled inbound/audit recording still pending restart-safe ACK work.
- Deferred ops remain out of docs: no split ingress, external queue, systemd/socket mutation, live Hermes config change, prompt change, retry UI, replay UI, or canary command.


## 2026-06-26 — Task 2 pending queue helpers

- `pending_deliveries` now stores only local SQLite queue state: inbound row reference, work kind, optional subscription, state/attempt timing, and last error. Raw payload remains in `inbound_events`; success dedup remains in `delivery_dedup`.
- `ControlLedger.record_inbound_event_and_enqueue_pending()` reuses the inbound canonical insert and `_connect()` transaction rollback, so simulated pending insert failure leaves no partial inbound or pending rows.
- Queue helpers intentionally stay narrow: due work is `pending`/`retry` with `next_attempt_at <= now`, and queue depth counts only `pending`/`retry`.


## 2026-06-26 — Task 2 QA fix: atomic delivery fanout

- `ControlLedger.record_inbound_event_and_enqueue_pending_deliveries()` records one inbound event and inserts all matched delivery subscriptions inside one `_connect()` transaction, so Task 3 can ACK only after the full fanout is durably queued.
- Multi-row pending insert failure rolls back both the inbound row and any earlier pending rows from the same call; routing-resolution remains the single `subscription IS NULL` helper path.
- `pending_deliveries` now has SQLite `CHECK` constraints for the exact allowed `kind` and `state` values plus delivery-vs-routing subscription nullability.


## 2026-06-26 — Task 4 accepted suppression paths

- `proxy.py` now records durable inbound rows immediately after valid HMAC + JSON parsing and before disabled/no-route/future-due decisions.
- Disabled sentinel handling uses the existing forwarding gate reason `legacy_disable_sentinel_present`, records a suppressed audit row, and returns `200` without route matching, Todoist parent lookup, pending queue rows, downstream POSTs, or replay semantics.
- Future-due and no-route accepted paths keep `deferred/due_in_future` and `unrouted/no_route` while adding inbound persistence and leaving `pending_deliveries` empty.


## 2026-06-26 — Task 3 ACK-first public webhook path

- Routed public webhook handling now uses `record_inbound_event_and_enqueue_pending_deliveries()` for matched enabled not-yet-successful subscriptions, so the accepted request path returns exact `200` after local SQLite handoff and performs zero downstream POSTs.
- Durable enqueue failure returns `503` with no pending rows and no downstream calls; invalid trust-boundary failures still short-circuit before ledger writes.
- Suppressed, no-route, future-due, and already-successful paths still record inbound rows with zero pending deliveries; `delivery_dedup` remains only a success ledger, not a pending-work source.


## 2026-06-26 — Task 3 QA fix: audit after durable persistence

- Matched-route audit writes are now buffered in local in-memory lists until inbound/pending persistence succeeds, preventing `routing_decisions` or `interactions` rows from leaking on enqueue failure.
- Skipped already-delivered and suppressed-target audit behavior is preserved after successful inbound persistence; no-forward-target paths persist inbound before flushing those audit rows.
- The enqueue-failure regression test now asserts `503`, no downstream POST, no inbound/pending rows, and no `routing_decisions` or `interactions` rows.


## 2026-06-26 — Task 3 QA fix: legacy event audit after durability

- `proxy.handle()` now creates the legacy `events` audit row only after durable inbound-only or inbound+pending persistence succeeds; durable persistence failure leaves no `events` row.
- Successful accepted paths still record `events` before semantic/routing/forward audit rows, preserving `event_row_id` links when `record_event()` itself succeeds.
- The enqueue-failure regression test now also asserts `events` count stays zero alongside inbound, pending, routing, and interaction tables.


## 2026-06-26 — Task 5 local delivery drain

- `proxy.drain_pending_deliveries()` is a one-shot async drain, not a daemon: it reads due `kind='delivery'` pending rows, reconstructs the exact webhook body from `inbound_events.raw_body`, forwards through existing `_forward()`, and updates only the pending row it processed.
- `delivery_dedup` remains the success ledger: `2xx` records dedup and marks pending `succeeded`; `3xx/4xx` marks `terminal_failed`; `5xx`/forwarding errors mark `retry`, increment `attempt_count`, and move `next_attempt_at` by a fixed testable delay.
- Partial fanout retry is row-local. Succeeded targets stay succeeded and are not re-forwarded; if a pending row already has a matching successful `X-Todoist-Delivery-ID` dedup entry, the drain records a skipped audit row and marks it succeeded without downstream POST.


## 2026-06-26 — Task 5 QA fix: drain forwarding compatibility

- Drain forwarding now preserves the same default upstream fallback used by the pre-ACK/drain delivery path and due poller: subscriptions missing from routing `upstreams` post to `http://127.0.0.1:8644/webhooks/<subscription>` instead of becoming local retry rows.
- Because `inbound_events.headers_json` stores only Todoist allowlisted inbound headers, `drain_pending_deliveries()` reconstructs the Hermes-facing `X-GitHub-Event` header from the stored payload `event_name` immediately before `_forward()`.
- The focused regression test uses a header-recording fake session to prove drain POST kwargs preserve `Content-Type`, `X-Todoist-Delivery-ID`, and `X-Todoist-Hmac-SHA256` while adding `X-GitHub-Event`.


## 2026-06-26 — Task 6 retryable note route resolution

- Retryable `note:added` parent lookup failures now ACK with `200` only after persisting the inbound webhook and a single `pending_deliveries.kind='routing_resolution'` row; no subscription delivery rows are created until parent context resolves.
- `drain_pending_deliveries()` now handles `routing_resolution` rows by reusing `_resolve_note_parent_context()`, `match_routes()`, forwarding gates, dedup checks, and the same pending delivery processing helper used by normal `kind='delivery'` rows.
- Route-resolution retry semantics mirror delivery retry semantics: retryable Todoist lookup failures increment `attempt_count` and back off `next_attempt_at`; non-recoverable parent/no-route outcomes mark the routing row `terminal_failed` with an audit interaction reason.


## 2026-06-26 — Task 7 CLI queue visibility

- `todoist-proxy status` now preserves the existing proxy/service/sentinel lines and appends `pending queue: <count>` when the ledger database exists.
- Queue visibility stays nonfatal: missing or invalid database paths print `pending queue: unavailable (...)`, and the service/sentinel state remains visible.
- The CLI validates the `pending_deliveries` table with a read-only SQLite query before calling `ControlLedger.pending_queue_depth()`, because that helper is intentionally best-effort and returns `0` on internal SQLite errors.
- Focused launcher tests stub `systemctl` via `PATH` and use temp `CONTROL_HOME`, avoiding live user systemd and production SQLite state.


## 2026-06-26 — Task 9 full regression and cleanup

- Regression evidence: `tests/test_ledger.py` passed 18 tests; proxy/forwarding/due-poller targeted tests passed 52 tests; CLI launcher tests passed 3 tests; full `python -m pytest -q` passed 132 tests; `python -m compileall /home/filipp/Projects/todoist-proxy` and `git diff --check` both exited 0.
- LSP diagnostics reported no Python errors for `control_ledger.py`, `proxy.py`, `due_poller.py`, `route_matcher.py`, or the changed tests directory.
- Scope audit found no dependency manifest changes, no systemd unit-file changes, no external broker/separate ingress/arbitrary-header-storage terms, and no `TODO`/`FIXME`/`HACK` markers. A stale README claim about Todoist-facing `502` retries and incomplete disabled-mode recording was corrected to match ACK-first local retry plus inbound/suppressed audit with no replay.

## 2026-06-26 — Final review reject cleanup

- Final cleanup removed the unused `_forward_and_record()` path and reverted repo startup to plain `web.run_app(..., host=..., port=...)`; socket activation stays explicitly out of scope unless a future plan owns systemd integration end to end.
