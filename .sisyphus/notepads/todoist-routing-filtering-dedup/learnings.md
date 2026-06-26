

## 2026-06-26T15:25:45+02:00 — Todoist webhook/task API research

- Official current docs are at `https://developer.todoist.com/api/v1/`. Context7 quota was unavailable, but Context7's public `developer_todoist_api_v1/llms.txt` mirrored the same source and the live Redoc page was fetched directly.
- Webhook payload envelope is documented with `event_name`, `user_id`, `event_data`, `version`, `initiator`, `triggered_at`, and optional `event_data_extra`. `event_data` is the modified entity; `item:*` events carry a Task, `note:*` events carry a Comment.
- Webhook HMAC is documented in request header `X-Todoist-Hmac-SHA256`: HMAC-SHA256 using the app `client_secret` as key and the whole request payload/raw body as message, base64 encoded. Official TypeScript SDK confirms raw-body verification and warns not to parse/re-serialize before hashing.
- Delivery ID: current official docs explicitly document `X-Todoist-Delivery-ID` as unique per webhook event notification; failed redelivery uses the same delivery ID. This is no longer merely a proposed/undocumented header based on the fetched docs.
- Delivery retry: official docs say callback must return HTTP 200; non-200 is failed delivery; retry after 15 minutes, at most three retries.
- Webhook task example includes `event_data.id`, `project_id`, `section_id`, `parent_id`, `added_by_uid`, `assigned_by_uid`, `responsible_uid`, `due`, `deadline`, `priority`, `content`, `description`, `labels`, `user_id`, and `url`.
- Task API/Sync task object fields relevant for note parent lookup: `project_id`, `section_id`, `parent_id`, `added_by_uid` (creator; shared projects; can be null for old tasks), `assigned_by_uid`, `responsible_uid`, and `due` are documented. Due object includes at least `date`, `string`, `is_recurring`, `lang`, and SDK also models optional `datetime` and `timezone`.
- `GET /api/v1/tasks/{task_id}` is documented as returning an active task by string ID; `GET /api/v1/tasks` can filter by `ids`, `project_id`, `section_id`, and `parent_id`.
- Official SDK nuance: `note:*` item-comment webhook payloads are typed as carrying `itemId` plus embedded full `item: Task`; project comments carry `projectId`. REST comment resource has exactly one of `itemId` or `projectId`. This suggests note routing may not need a separate task lookup if live payloads include embedded `item`, but fallback lookup by `item_id`/`itemId` remains safe for compatibility.
- Dedup impact: preferred key should be `(source='todoist', subscription/route target, X-Todoist-Delivery-ID)` when the header is present. Because retries preserve the same delivery ID, this suppresses Todoist retry duplicates without suppressing distinct events about the same task. Fallback if header missing should combine event identity and stable payload data, e.g. `(subscription, event_name, event_data.id, triggered_at/version if present, sha256(canonical/raw payload))`; include subscription/target because fanout sends one Todoist event to multiple downstream agents.
- Do not dedup only on task ID: recurring tasks and multiple event types (`item:added`, `item:updated`, `item:completed`, `note:added`) can legitimately reuse the same task ID.
- Sources: Todoist API v1 docs `https://developer.todoist.com/api/v1/#tag/Webhooks/Request-Format`, `https://developer.todoist.com/api/v1/#tag/Webhooks/Configuration`, `https://developer.todoist.com/api/v1/#tag/Tasks`, `https://developer.todoist.com/api/v1/#tag/Tasks/operation/get_task_api_v1_tasks__task_id__get`; official SDK commit `323359fd714f7c895c29a3fb4f39be4edd5bf9e8` files `src/utils/webhook-parser.ts`, `src/types/tasks/types.ts`, `src/types/webhooks/comments.ts`, `src/types/comments/types.ts`.

## 2026-06-26T15:25:24+02:00 — external patterns: per-target webhook idempotency

- RichardAtCT/claude-code-telegram shows the smallest SQLite dedup primitive: `delivery_id TEXT UNIQUE` plus `INSERT OR IGNORE` and `SELECT changes()` to tell whether this process won the insert. For todoist-proxy, adapt the unique key to `(event_key, subscription)` rather than global event-only dedup.
- Baserow stores webhook call results keyed by `(event_id, batch_id, webhook, event_type)` and uses `update_or_create`, which supports the per-target/per-event shape and avoids duplicate call log rows for the same target/event.
- Skyvern tests bounded async HTTP retry semantics: 2xx returns immediately, 5xx/429/network/timeouts retry, 400/401/404 do not retry, Retry-After is honored/capped, and sleeps are monkeypatched for deterministic tests.
- TranscriptionSuite is useful mainly for invariants and tests: status transitions are committed around HTTP delivery, 2xx is success, non-2xx/timeouts fail, mock aiohttp receiver tests delivery behavior, and crash/in-flight recovery is explicit. Do not import its queue design; this repo only needs a success ledger.
- Pitfall: if a success ledger row is inserted before downstream 2xx, Todoist retry may skip a target that never succeeded (lost retry). Insert success only after `2xx`/accepted downstream result; failed targets must stay absent from the success ledger.
- Testing pattern for this repo: configure two fake targets, first returns 200 and second returns 500; assert first run records only target A success and returns retryable failure; second run skips A, resends only B, and records B on 2xx. Repeat the second run to assert both are skipped.

## 2026-06-26T15:33:18+02:00 — Task 1 route matcher

- Added pure `route_matcher.py` with centralized subscription→agent and agent→Todoist UID constants, a frozen `MatchedRoute`, legacy list broadcast support, and fail-closed conditional route matching.
- Conditional matching preserves Todoist IDs as opaque strings, treats `"0"` as present, uses `responsible_uid`/`assignee_id` before section fallback, only allows section fallback when responsible is absent, and limits creator fallback to `item:updated`, `item:completed`, and `item:uncompleted`.
- Mention alias helper uses regex boundary checks so aliases like `Max` do not match substrings like `Maximum`.
- Verification: `python -m pytest tests/test_route_matcher.py -q` passed with 13 tests; evidence saved to `.sisyphus/evidence/task-1-route-matcher.txt`.

## 2026-06-26 — Task 2 delivery dedup ledger

- Added `delivery_dedup` to `ControlLedger.initialize_schema()` with per-target success rows and two uniqueness modes: Todoist webhook delivery IDs dedup by `(source, event_name, delivery_id, subscription)`, while fallback rows dedup by source/event/entity/parent/due/payload hash plus subscription.
- `build_delivery_identity()` keeps Todoist IDs as strings, uses comment `item_id` as parent task context, derives due values from explicit caller input or due objects, and leaves payload hash fallback sensitive to payload changes.
- Verification: `python -m pytest tests/test_ledger.py -q` passed with 6 tests; evidence saved to `.sisyphus/evidence/task-2-dedup-success.txt`. `python -m compileall control_ledger.py tests/test_ledger.py` also passed.

## 2026-06-26 — Task 4 due-poller matcher + per-target dedup

- `due_poller.py` now builds the synthetic due `item:added` event before routing, sends it through `route_matcher.match_routes()`, and evaluates control gates only for matched targets.
- Due-poller delivery dedup is per target and due value: already-successful targets are skipped before `_unblock()`/`_deliver()`, failures remain retryable, and global `fired_due` is written only when at least one enabled target exists and every enabled target is already or newly successful.
- Bootstrap and dry-run still avoid interaction-ledger schema/event/delivery rows, unblock writes, downstream posts, and fired-state mutation.
- Verification: `python -m pytest tests/test_route_matcher.py tests/test_due_poller_ledger.py -q` passed with 23 tests; evidence saved to `.sisyphus/evidence/task-4-due-poller-conditional.txt`. `python -m compileall due_poller.py tests/test_due_poller_ledger.py` also passed.

## 2026-06-26 — Task 3 proxy conditional routing + delivery dedup

- `proxy.py` now routes Todoist webhook candidates through `route_matcher.match_routes(...)` after future-due deferral and project resolution; legacy flat-list routes still broadcast, while conditional routes only create control/routing ledger rows for matched targets.
- Delivery dedup uses `X-Todoist-Delivery-ID` when present and records `delivery_dedup` rows only after downstream status `< 300`; retries skip already-successful targets and retry only failed matched/enabled targets.
- Proxy retry semantics changed from “502 only if every enabled target failed” to “502 if any matched enabled not-already-delivered target has a 5xx/timeout/error,” enabling Todoist retries to fill partial fanout failures safely.
- Verification: `python -m pytest tests/test_route_matcher.py tests/test_proxy_webhook.py tests/test_forwarding_controls.py -q` passed with 35 tests; evidence saved to `.sisyphus/evidence/task-3-proxy-conditional.txt`. `python -m compileall proxy.py tests/test_proxy_webhook.py tests/test_forwarding_controls.py` also passed.

## 2026-06-26 — Task 5 note explicit mention before parent fallback

- `note:added` conditional routing is now two-phase: configured mention aliases across the project win first; parent-task relevance is only considered when no configured alias matches. This prevents a parent-assigned agent from also receiving a comment explicitly directed to another agent.
- Proxy note routing builds parent context from embedded `event_data.item`/`event_data.task` when present, otherwise looks up `GET /api/v1/tasks/{item_id}` only when needed. Project-level notes that already include `project_id` and an explicit mention route without parent lookup.
- Parent context used for fallback includes `project_id`, `section_id`, `responsible_uid`/`assignee_id`, and creator fields. Missing/deleted parents fail closed for conditional routes; retryable lookup failures return 502 so Todoist can retry if routing cannot be computed.
- Verification: `python -m pytest tests/test_proxy_webhook.py tests/test_route_matcher.py -q` passed with 34 tests; evidence saved to `.sisyphus/evidence/task-5-note-explicit-mention.txt`. `python -m compileall proxy.py route_matcher.py tests/test_proxy_webhook.py tests/test_route_matcher.py` also passed.
- Related compatibility check: `python -m pytest tests/test_forwarding_controls.py -q` passed with 8 tests after preserving the existing `_resolve_project_id` monkeypatch seam for older control tests.

## 2026-06-26 — Task 6 docs update

- README now documents legacy flat broadcast routes, conditional per-subscription routes for LowKeyCodes, event-specific matching semantics, per-subscription delivery dedup, partial proxy retry behavior, and the out-of-scope route UI and prompt work.
- state.md now marks route filtering as implemented in repo code/tests while keeping live routing config migration, Hermes prompt updates, webhook subscription changes, and prompt-level dedup/cooldown removal as separate operational follow-ups.
- Verification: `git diff --check` passed with empty output saved to `.sisyphus/evidence/task-6-docs-diff-check.txt`. Markdown LSP diagnostics could not run because no .md LSP server is configured in this environment.

## 2026-06-26 — Task 7 full regression and hardening

- Full Task 7 regression passed: route matcher focused tests (`16 passed`), proxy/control focused tests (`26 passed`), due-poller focused tests (`10 passed`), full pytest (`102 passed`), compileall, and `git diff --check` all exited 0 with evidence saved under `.sisyphus/evidence/task-7-*`.
- Diff audit found no tracked/generated cache, runtime DB, env, or secret-pattern files. Filesystem checks found no repo-local `__pycache__`, `.pyc`, `.db`, `.sqlite`, or `.env` artifacts after verification.
- Behavioral audit confirmed route-filtered targets only produce ledger/control rows after matching, legacy flat routes still broadcast, conditional owner/section routing minimizes fanout, and per-target dedup retries failed targets without resending already-successful targets.
