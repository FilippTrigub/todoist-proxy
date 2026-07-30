# Proxy State & Pending Work

_Last synced with repo: 2026-07-15._

## Project-move item:updated → synthetic item:added (new, uncommitted 2026-07-15)

**Problem:** tasks *moved* into a routed project (e.g. dragged from the personal
Todoist inbox into Trigub Technologies Inbox) never triggered agents. Todoist
fires `item:updated` for a project move — never `item:added` — and
`hausmeister-inbox` only subscribes to `item:added`/`note:added`, so Hermes
silently accepted-and-dropped the forwarded event (HTTP 200, no agent, no log
line). Diagnosed 2026-07-15 from the interaction ledger: 12 `item:updated`
move events into the Inbox since 07-13, zero `item:added`.

**Fix (`proxy.py`):** immediately after payload parsing in `handle()`, if
`event_name == "item:updated"` and `event_data_extra.old_item.project_id`
differs from the new `event_data.project_id` (`_moved_from_project_id()`), the
event is rewritten in place to a synthetic `item:added`: `event_name` replaced
in the payload, `_synthetic: true`, `_trigger: "project_move"`, and
`_moved_from_project_id` added to `event_data`, and the forwarded body
re-serialized. Everything downstream then follows the normal item:added path
for **all** routes/agents automatically: future-due deferral (due_poller picks
the task up later), item:added route matching (responsible/assignee →
unassigned-section fallback, **no creator fallback**), ledger recording,
`X-GitHub-Event: item:added`, and `task_assigned` semantic extraction.
Malformed/missing `old_item` fails closed to normal item:updated handling.
Same-project `item:updated` is untouched. No Hermes subscription or prompt
changes needed; no self-trigger loop risk since agent edits don't change
`project_id`.

**UI (`control_ui.py`):** Routing rules tab — new global-exception bullet
documenting the rewrite, and the conditional-route event tag now reads
`item:added & due-poll & project-move` (with the creator-not-checked caveat
extended to project-move rewrites).

**Tests:** 5 new in `test_proxy_webhook.py` (rewrite + markers persisted,
future-due deferral on moves, same-project update not rewritten, no creator
fallback after rewrite, responsible-uid match posts only that agent).
`python -m pytest -q` → 190 passed. Verified live 2026-07-15: service
restarted, signed synthetic move payload → ledger row `item:added` /
`_trigger=project_move`, journal line "moved project … — treating as
item:added".

---

_Previous sync: 2026-07-11. Branch `main`, ahead of `origin/main`, plus
uncommitted working-tree changes described below: the adaptive report-cadence
trigger for Max (now also called the **spark mechanism** in the operator UI),
control-UI panels for its live parameters and countdown, `todoist-proxy spark`
CLI controls, and removal of the control-UI token gate. Everything from the prior 2026-07-02 sync
(delegation-tree drill-down, comment-mention loosening) is already committed
— see `## Delegation-tree drill-down` / `## Comment-mention loosening` below,
kept for historical detail._

## Adaptive report-cadence trigger for Max (new, uncommitted this sync)

Max's business-state check-in prompt (stored at
`lowkeycodes/Filipps control prompt/Report current LowKeyCodes business
state to Filipp.md` in the Obsidian vault) used to run on a fixed-cadence
recurring Todoist task. Filipp asked for the cadence to become adaptive —
fire more often the further behind the €1000 MRR target the company is and
the more stalled the ledger looks. The automatic formula is bounded to
[1h, 1 week], then the manual speed override can deliberately push the
effective interval outside those bounds. Timing is driven by an
explicit mathematical function rather than Todoist's recurrence engine or
ad hoc LLM judgement. Confirmed with Filipp up front: no Todoist due-date
rescheduling at all; a dedicated poller owns timing itself and fires a
synthetic event directly with the composed prompt + fresh data baked in.
MRR comes from Stripe directly (not Max's self-reported figure).

- `report_cadence.py` (new): pure formula + Stripe fetch + prompt
  composition. `CadenceParams` dataclass now holds 9 tunables (MRR target,
  events baseline, 3 pressure weights, min/max interval hours,
  `speed_multiplier`, legacy-revenue cutover date). `compute_interval_hours(mrr_current, mrr_projected,
  events_24h, params=...)`: three 0–1 pressure sub-scores — `gap` (today's
  shortfall), `shortfall` (30d-forward-projected shortfall), `stagnation`
  (how quiet the ledger's been in 24h) — combined into a weighted pressure
  `P`, mapped to `T_hours = t_max * (t_min/t_max) ** P` (exponential
  interpolation so the 1h–168h range doesn't collapse near one extreme;
  P=0 → 168h, P=1 → 1h), then divides by `speed_multiplier`. That manual
  speed override is deliberately not reclamped after division, so operators
  can push the effective interval outside the formula bounds when needed.
  `fetch_mrr_signals` sums qualifying Stripe charges
  over trailing/leading 30-day windows, excluding pre-`2026-06-10` legacy
  revenue per `finance/scoreboard.md`'s documented cutover (that account
  also holds an unrelated prior business's charges).
- `report_cadence_poller.py` (new, sibling to `due_poller.py`): loads any
  saved parameter overrides from `todoist-control.json`'s `report_cadence`
  key (falls back to code defaults if invalid), computes the interval, and
  — once that interval has elapsed since the last fire (tracked in
  `~/.hermes/state/report_cadence.db`) — builds a synthetic
  `item:added`-shaped event (fixed id `report-cadence-max`,
  `responsible_uid` = Max's uid `59328091`, prompt text in `description`)
  and delivers it via the exact same in-process routing + `_deliver` POST
  mechanism `due_poller.py` already uses (`~/.hermes/todoist-routing.json`
  → `max-lowkeycodes` upstream `http://127.0.0.1:8641`), bypassing
  `proxy.py`'s HTTP ingestion entirely — same as the due poller. Records
  through `ControlLedger` (`source="report_cadence"`) for ledger/routing
  visibility (`interaction_kind="report_cadence_triggered"`; the primary
  timeline still filters to task/comment/due semantic rows). Supports
  `--dry-run`. Follow-up this session: each non-dry
  evaluation now also persists a compact scheduler snapshot under SQLite
  `meta['last_status_json']` (`last_evaluated_at`, `last_fired_at`,
  `interval_hours`, `next_fire_at`, signals, params, status), so the local UI
  can render a countdown without calling Stripe or recomputing poller logic.
  Dry runs intentionally do **not** mutate this status snapshot.
- `control_ledger.py`: added `ControlLedger.count_events_since(since_iso)`
  — `SELECT COUNT(*) FROM events WHERE received_at >= ?`, best-effort
  (returns 0 on a missing/corrupt DB) since it's one formula input, not a
  source of truth. Follow-up this session: `evaluate_forwarding(...,
  source="report_cadence")` now honors `global.spark_enabled`; when false,
  report-cadence delivery is suppressed with reason `global_spark_disabled`.
  This blocks manual poller runs too, not only the systemd timer.
- Verified with `--dry-run` against real production Stripe/ledger data:
  MRR correctly resolves to €0.00 (legacy charges excluded, matching
  `finance/scoreboard.md`), 27 events in the last 24h, computed interval
  ~2h. Both the "not due yet" and "due" decision paths log correctly.
- `~/.hermes/todoist-proxy.env`: added `STRIPE_SECRET_KEY`, copied from
  `/home/filipp/Projects/flashme/.env.local` — confirmed with Filipp this
  is the same Stripe account Max already uses for LowKeyCodes snapshots
  (Max's own profile env at `~/.hermes/profiles/max/.env` has no Stripe key
  of its own). Value never echoed to chat; copied via a Python script, not
  `grep`, after an RTK hook was found silently rewriting `grep` output into
  a truncated summary format that corrupted the env file on the first
  attempt (caught and fixed before any bad value was left in place).
- systemd units created but **not enabled**: `~/.config/systemd/user/
  report-cadence-poller.{service,timer}` (mirrors
  `todoist-due-poller.{service,timer}` exactly, `OnUnitActiveSec=10min`).
- Not done yet, deferred pending Filipp's go-ahead since both are live-
  production actions: enabling the spark timer and a true one-shot live-
  delivery test (without `--dry-run`) confirming Max's downstream gateway
  actually starts a session from this synthetic event. The preferred operator
  command is now `todoist-proxy spark on`, which both writes
  `global.spark_enabled=true` to `todoist-control.json` and runs
  `systemctl --user enable --now report-cadence-poller.timer`; `todoist-proxy
  spark off` writes `global.spark_enabled=false` and disables/stops the timer.
  If spark is off and the poller is manually run while due, it records
  `status="suppressed"`, returns success, does not call downstream delivery,
  and does not advance `last_fired_at`, so re-enabling spark does not silently
  skip an overdue check-in.

## Report-cadence control-UI panel (new, uncommitted this sync)

`control_ui.py` gained a first-class **Spark controls** section, rendered above
Timeline, for the adaptive report-cadence parameters. The general Controls
section now stays below Timeline and keeps the non-spark forwarding gates, per
Filipp's follow-up ask for manual control over the function via UI.

- `GET/POST /api/report-cadence/config` — GET returns `{defaults,
  overrides, effective, config_status}`; POST accepts a partial override
  dict (strict validation via `report_cadence.parse_cadence_overrides` /
  `validate_cadence_params`) or `{"reset": true}`, persisted into
  `todoist-control.json`'s `report_cadence` key, audited via
  `ControlLedger.record_config_audit`.
- Live SVG curve (frequency/checks-per-day vs. MRR, holding projected=current
  and events=baseline) plus a plain-text equation block with current values
  substituted in, including `effective_h = interval_h / speed_multiplier(...)`
  — both recomputed client-side on every keystroke via a JS mirror of
  `compute_interval_hours` (comment-flagged to keep in sync, kept client-side
  rather than a network round trip so it's genuinely real-time). The manual
  speed override is rendered as a range slider.
- Auto-save, 500ms debounce after the last edit, no manual Save button.
- Follow-up this session: the same panel now has a large **Next spark event**
  countdown card. `GET /api/report-cadence/status` reads
  `~/.hermes/state/report_cadence.db` (or `REPORT_CADENCE_DB`) and returns the
  poller's persisted `last_status_json`, augmented with server-side
  `server_now`, `remaining_ms`, `seconds_until_next_fire`, and `due`. The
  browser ticks from that server-provided remaining duration using
  `performance.now()` instead of trusting the client wall clock. Missing DB →
  `not_initialized`; legacy DB with only `last_fired_at` →
  `schedule_unavailable`; no Stripe/network calls happen in the UI request
  path.
- Follow-up this session: `spark_enabled` is visible/editable in the dedicated
  Spark controls section. It still comes from `/api/config/effective`'s global
  gates, but it no longer appears in the lower general Controls section.
- Two real bugs found and fixed while building this, both verified live via
  `agent-browser` against an isolated scratch `CONTROL_HOME` (never against
  the production `todoist-control.json`):
  1. Round values (e.g. `100`) silently failed to save. Root cause: a
     mismatched HTML5 `step`/`min` combo on the number inputs (e.g.
     `step="1" min="0.01"`), which fails native browser step-validation
     before the request ever reaches the server — the server-side
     validation was fine all along. Fixed with `step="any"` on every
     numeric field.
  2. Autosave originally POSTed the *entire* form on every save, so
     editing one field silently marked all tunables as "override". Fixed by
     tracking only the specific `input`-event field names actually touched
     (`cadenceDirtyFields`) and sending only those.

## Control-UI token gate removed (uncommitted this sync)

Filipp asked why write endpoints needed a pasted control token given the
UI is loopback-only (`127.0.0.1`) by default. This is the exact reasoning
already recorded as "confirmed intentional" on 2026-07-01 (see item #4
below) — it must have been silently reintroduced at some point since, and
this session's new cadence-config endpoint mirrored that existing (wrong)
pattern without question. Removed again, this time end-to-end so it can't
silently reappear the same way:

- `control_ui.py`: deleted `_authorized`, `resolve_token`, `TOKEN_HEADER` /
  `TOKEN_ENV` / `TOKEN_FILE_ENV` / `DEFAULT_TOKEN_FILE_NAME`, the `token`
  parameter threaded through `handle_api_request` / `make_handler` /
  `create_server` / `main()`, and the now-unused `secrets` import. JS
  `controlTokenHeaders()` / `promptForControlToken()` / the sessionStorage
  token cache are gone; `postToggle` / `postCadenceConfig` now do a plain
  `fetch` with just `Content-Type: application/json`.
- Both `/api/config/toggle` and `/api/report-cadence/config` POST succeed
  with zero auth headers now. `GET /api/report-cadence/status` also succeeds
  without auth and is read-only (`POST` returns 405).
- Tests: `tests/test_ui_security.py` rewritten — the two "requires custom
  token header" tests became "succeeds without any auth header" tests
  (kept as regression coverage against a gate silently reappearing again);
  the token-resolution/token-file tests were deleted outright since
  `resolve_token` no longer exists. `tests/test_ui_api.py` and
  `tests/test_ui_playwright.py` had their now-meaningless `token=`
  kwargs/assertions dropped.
- `README.md`'s "Writes ... require the `X-Todoist-Control-Token` header"
  paragraph corrected to state there is no auth on any endpoint.

## Delegation-tree drill-down (committed in `ef61ca5`)

Adds a "process tree" view to the Timeline: clicking a `task_assigned` arrow
(or its `task <id>` label), or typing a task ID into the new search box in
the Timeline toolbar, swaps the swim-lane graph for a nested delegation tree
— e.g. Filipp assigns task T to Max, Max creates a subtask of T assigned to
Smith — rooted at the top-most ancestor, with the clicked/searched task
highlighted. A "← Back to timeline" button restores the normal graph.

Confirmed with the user before implementing: delegation is always modeled as
a Todoist **subtask** (the delegated task's `parent_id` points back to the
task the delegator was given), never an unrelated top-level task. That's the
only deterministic signal available without extra API calls, and it's
already present for free in the raw `item:added` webhook payload.

Changes, bottom-up:

- `interaction_extractor.py`: `SemanticInteraction` gained `parent_task_id`
  (default `""`); `_extract_item_added` now reads `event_data["parent_id"]`
  (falling back to `parentId`) into it. `note:added` interactions leave it
  empty — comments aren't delegation edges.
- `control_ledger.py`: `interactions` table gained a `parent_task_id` column
  via the existing `INTERACTION_TIMELINE_COLUMNS` auto-migration path (same
  mechanism already used for `actor`/`target`/`interaction_kind`/`confidence`
  — no new migration code needed). `record_interaction(...)` takes an
  optional `parent_task_id` kwarg and persists it.
- `proxy.py`: `_record_semantic_interactions` threads
  `interaction.parent_task_id` through to `ledger.record_interaction(...)`.
  Due-poller `due_triggered` rows are unaffected — those never went through
  `extract_interactions` and aren't tree edges.
- `control_ui.py`:
  - `_task_assigned_rows()` / `_build_task_tree()`: loads all `task_assigned`
    rows (capped at 5000), walks `parent_task_id` up to the root ancestor,
    then rebuilds the tree top-down. Cycle-guarded (`MAX_TASK_TREE_DEPTH`).
    **Superseded same-day, see "Comment-mention loosening" below** — this
    function now also considers `comment_mentioned` rows.
  - New `GET /api/task-tree?task_id=<id>` → `{"task_id", "tree"}` or `404` if
    the task was never seen as a `task_assigned` target.
  - SVG rows (both server-rendered `_render_timeline_svg` and the client
    `renderSvg()`) are now wrapped in `<g class="timeline-row [has-task-id]"
    data-task-id="...">` for a single clickable hit area per row — purely
    additive, doesn't change any existing `<path>`/`<circle>` attributes the
    tests assert on.
  - New toolbar markup (search input + "View tree" + "← Back to timeline",
    added alongside the untouched "Expand timeline" button) and JS
    (`showTaskTree`, `showTimelineView`, `bindTreeControls`); `refresh()` now
    skips re-rendering `#timeline-frame` while in tree mode so the 5s
    auto-refresh doesn't stomp an open tree.
- Tests added: `test_interaction_extractor.py` (parent_id extraction),
  `test_ledger.py` (column persistence), `test_proxy_webhook.py`
  (`task-max-to-smith-subtask-001` end-to-end), `test_ui_api.py`
  (`_build_task_tree` via the API: mid-chain focus, unknown task → 404,
  missing param → 400).
- Verified visually: started `control_ui.create_server(...)` against a
  seeded 3-level chain and drove it with `agent-browser` — click-to-drill,
  search-to-drill, and back-to-timeline all render correctly, with the
  focused node getting a mint highlight border.

## Comment-mention loosening (committed in `ca2c8b5`)

Follow-up after the user reported the tree "does not work, the rules are too
strict." Root cause (written up in full in the prior chat turn, kept short
here): `_build_task_tree` only ever recognized `task_assigned` rows as nodes,
and Todoist assignment frequently happens via `item:updated` (not captured at
all by `extract_interactions`, which only handles `item:added`/`note:added`)
or a task can simply lack a `responsible_uid`/`creator_uid` at creation —
either way, no `task_assigned` row exists, so the child became an orphaned
root with its true parent invisible.

Fix implemented (not a fix for `item:updated` yet — see below): fold
`comment_mentioned` rows into the tree as a second, already-captured
delegation signal, since Hermes agents already hand off tasks via `@Name`
comment mentions on the same task, not only by creating subtasks.

- `control_ui.py`: `_task_assigned_rows` → `_task_delegation_rows`, now
  selecting `interaction_kind IN ('task_assigned', 'comment_mentioned')`
  (`TASK_TREE_EDGE_KINDS`). `_build_task_tree` reworked:
  - A task becomes a node (`handoffs_by_task[task_id]`) if it has **either**
    kind of row — a `comment_mentioned`-only task (never had its own
    `task_assigned` row) can now be its own tree node/root.
  - Every row for a task id is kept, sorted by `created_at`, with
    consecutive-identical-row dedup (same actor/target/kind/reason back to
    back — e.g. duplicate webhook redelivery) — not "last one wins" like
    before, because comment mentions are a genuine sequence of handoffs on
    the *same* task, not reassignment noise.
  - The subtask parent/child link (`parent_by_task`) is unaffected: it still
    only ever comes from a `task_assigned` row's `parent_task_id`, because
    `note:added` extraction has no parent concept.
  - Node shape changed from a single `actor`/`target`/`status`/`reason`/
    `created_at` to a `handoffs: [...]` list — each entry has its own
    `actor`/`target`/`kind`/`confidence`/`status`/`reason`/`created_at`. A
    task assigned to Max and then mentioned to Smith in a comment on the
    *same* task now renders as one node with a 2-row handoff sequence,
    instead of requiring (or faking) a subtask.
  - `GET /api/task-tree` 404 message updated to "never seen as a
    task_assigned target or a comment_mentioned target".
- JS: `renderTreeNode`/new `renderTreeHandoff` render each task as a bordered
  group (`.tree-node-handoffs`) containing one row per handoff, dashed
  divider between rows, task-id label only on the last row of the group.
- Tests added in `test_ui_api.py`:
  `test_task_tree_comment_mention_extends_handoff_without_a_subtask` (same
  task, task_assigned then comment_mentioned, no new node created) and
  `test_task_tree_mention_only_task_becomes_its_own_node_without_task_assigned`
  (comment_mentioned with zero task_assigned rows still resolves). Existing
  mid-chain test updated to assert `handoffs` instead of the old flat fields.
- Verified visually again with a live `create_server` + `agent-browser`
  session: a root task showing a 2-row stacked box (assigned, then
  mentioned) with the task-id label correctly only on the last row, and a
  mention-only task resolving as its own highlighted root.

Still not fixed (explicitly out of scope for this pass, flagged to the user
as the likely #1 remaining cause of "too strict" if mentions alone don't
cover it): `item:updated` reassignment is still never captured by
`extract_interactions` at all. If most real delegation happens by assigning
an already-created task rather than mentioning or creating a subtask, trees
will still come up short.

### Previously committed, same session

`9a494de` (context-packet enrichment + background drain loop + Session
insights/Routing rules tabs) and `6d249fe` (pixel/techy/minimal UI restyle)
are already on `main` — see their commit messages. The write-ups below for
those are kept only where the detail isn't obvious from the diff.

### 1. Task context-packet enrichment (`proxy.py`) — committed in `9a494de`

Before a webhook payload is forwarded (both on first delivery in `handle()`
and on retry in `_process_pending_delivery`), the proxy now calls
`_enrich_payload_with_context_packet(...)`, which:

- Looks up the current task via `_lookup_task_summary`.
- If the task has a `parent_id`, looks up the parent task and its other
  children via `_lookup_child_task_summaries` (siblings).
- Builds a `context_packet` (task tree + summary) and injects it into
  `event_data.context_packet` in the payload actually sent downstream. The
  original `raw_body` stored in `inbound_events` is untouched; only the
  forwarded copy (`forward_body` / `enriched_body`) carries the packet.
- New test: `tests/test_proxy_webhook.py::test_parent_task_context_packet_is_persisted_for_delivery`
  asserts the packet shape (`status`, `task_tree.parent_task_id`,
  `task_tree.parent_task`, `task_tree.siblings`, `summary` containing
  `"subtask"`).

This is additive to the already-committed ACK-first / restart-safe delivery
path (commit `6991a19`) — `drain_pending_deliveries` (used by both the retry
drain and the new background loop below) was already in place before this
diff; this diff only adds the enrichment call sites plus a background loop
to invoke it periodically instead of relying solely on request-time/manual
draining.

### 2. Background drain loop (`proxy.py`) — committed in `9a494de`

`on_startup` now spawns `app["drain_task"] = asyncio.create_task(_drain_loop(app))`,
which calls `drain_pending_deliveries(session, limit=50)` every
`TODOIST_DRAIN_INTERVAL_SECONDS` (default `2`) forever, logging and
continuing on exceptions. `on_shutdown` cancels it and awaits cleanly.

### 3. Control UI additions (`control_ui.py`) — committed in `9a494de`; restyled in `6d249fe`

- **Session insights tab**: new `/api/langfuse` endpoint
  (`_fetch_langfuse_traces`) reads Langfuse credentials from
  `~/.hermes/.env` (`LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY`), fetches traces
  tagged `platform:webhook`, and the page renders per-profile cost/latency
  aggregates plus a recent-sessions table, polled every 30s client-side.
- **Routing rules tab**: reads `~/.hermes/todoist-routing.json` (and
  `webhook_subscriptions.json` for per-subscription event lists) directly
  in the UI process and renders human-readable routing rules per project
  (broadcast vs conditional, responsible/section/creator matches, mention
  aliases), with static project/uid/section name lookup tables
  (`_PROJECT_NAMES`, `_UID_NAMES`, `_SECTION_NAMES`) hardcoded for the
  current live setup. Purely read-only, hot-reloaded per page load.
- Nav bar grew from 3 tabs to 5 (`Controls`, `Timeline`, `Event ledger`,
  `Session insights`, `Routing rules`). Follow-up this session: the current
  rendered order is now `Spark controls`, `Timeline`, `Controls`, `Event
  ledger`, `Session insights`, `Routing rules`.

### 4. `/api/config/toggle` auth removed (confirmed intentional, 2026-07-01)

The diff removed the token-gate UI (`#token-input` control-toolbar) *and*
the server-side check in `handle_api_request`:

```python
if parsed.path == "/api/config/toggle":
    if method != "POST":
        return _json_response(405, {"error": "method not allowed"})
    return _toggle_config(control_home, body)   # <-- no _authorized(headers, token) check anymore
```

`_authorized`, `TOKEN_HEADER`, and `resolve_token` still exist and are used
elsewhere, but nothing calls `_authorized` before allowing a write anymore.
Confirmed with the user: intentional, on the grounds that the control UI is
local-only (binds `127.0.0.1` only) so a control token adds no real
protection. This still contradicts README.md's documented behavior
("Writes to `POST /api/config/toggle` require the `X-Todoist-Control-Token`
header") and leaves `tests/test_ui_security.py::test_post_toggle_requires_custom_token_header`
asserting the old (now removed) behavior:

```
tests/test_ui_security.py::test_post_toggle_requires_custom_token_header
  assert missing.status == 403
  E   assert 200 == 403
```

Follow-up, done 2026-07-11: at some point after this 2026-07-02 note the
`_authorized` check was reintroduced on `/api/config/toggle` (and this
session's new `/api/report-cadence/config` endpoint copied that same
pattern without question) — see `## Control-UI token gate removed` above
for the full removal, this time including deleting `_authorized`,
`resolve_token`, `TOKEN_HEADER`, and all token-file plumbing outright, plus
the README.md correction.

### 5. Second, pre-existing test failure (unrelated regression, just stale)

```
tests/test_ui_playwright.py::test_control_page_has_exact_main_sections_and_gate_controls
  assert parser.main_sections == ["Controls", "Timeline", "Event ledger"]
  E   AssertionError: assert [...'Routing rules'] == [...'Event ledger']
```

Expected — the test hasn't been updated for the two new tabs added in this
diff. Needs the assertion list extended to include `Session insights` and
`Routing rules` once the tab addition is intentional/final.

Resolved by the time of the 2026-07-11 sync — `test_ui_playwright.py` now
asserts all five tabs and passes; unclear which commit fixed it since it
predates this session.

## Test suite status (2026-07-02 sync, historical)

```
python -m pytest -q
145 passed, 2 failed
  - test_ui_playwright.py::test_control_page_has_exact_main_sections_and_gate_controls  (stale assertion, see #5)
  - test_ui_security.py::test_post_toggle_requires_custom_token_header                  (real regression, see #4)
```

The delegation-tree and comment-mention tests are all in the 145 passing. The
`+5` over the previous sync's 140 includes tests from the concurrent
`route_matcher.py` work mentioned at the top of this file, not just this
session's own additions.

Both failures above are resolved as of the 2026-07-11 sync (see #4/#5
follow-up notes and `## Test suite status (2026-07-11 sync)` below).

## Test suite status (2026-07-11 sync)

```
python -m pytest -q
184 passed
```

All green — no known failing or stale tests at this sync. The count now
includes the follow-up coverage for the spark mechanism: scheduler status
snapshots/countdown endpoint, `todoist-proxy spark on/off/status`,
`global.spark_enabled` forwarding gates, and the manual-poller suppressed path
when spark is off.

---

## Filtering: proxy vs prompt (implemented in repo, config migration separate)

The proxy and due poller support per-subscription route conditions in code and tests.
Legacy flat routes still work and broadcast every project event to all listed
subscriptions:

```json
"6gmpjVFv2wVG7XJQ": ["max-lowkeycodes", "abra-lowkeycodes", "smith-lowkeycodes"]
```

Conditional routes use a per-subscription object, evaluated before forwarding:

```json
"routes": {
  "6gmpjVFv2wVG7XJQ": {
    "max-lowkeycodes":   {"responsible_uids": ["59328091"], "section_ids": ["6gpFcCwF29V6QXxx"]},
    "abra-lowkeycodes":  {"responsible_uids": ["15795569"], "section_ids": ["6gpFcCvfqGxWcqwx"]},
    "smith-lowkeycodes": {"responsible_uids": ["29584133"], "section_ids": ["6gpFcCxmc39r8MrQ"]}
  }
}
```

Implemented behavior:
- `item:added` and due-poller synthetic `item:added` match responsible/assignee first,
  then unassigned section fallback. They do not use creator fallback.
- `item:updated`, `item:completed`, and `item:uncompleted` also allow creator fallback.
- `note:added` routes explicit mention aliases first. If no alias matches, it falls back
  to parent-task relevance. Conditional note routes fail closed if parent context cannot
  be resolved.
- Successful deliveries are deduped per subscription. Proxy retries skip targets already
  recorded as successful and retry only failed matched targets.

Not done here: live `~/.hermes/todoist-routing.json` migration, Hermes prompt updates,
webhook subscription changes, and removal of prompt-level `handled_task_ids` or recurring
cooldown safeguards. Those are operational follow-ups if the live setup still uses flat
routes or prompt-level relevance checks.

---

## Restart-safe ACK-first delivery (done, committed in `6991a19`)

Implemented per `restart-safe-ack-plan.md` (see `.sisyphus/plans/restart-safe-ack.md`
for the full plan this was executed from): the public webhook handler durably
records inbound events + pending deliveries in SQLite before returning `200`,
including for disabled-forwarding, no-route, future-due, and downstream-failure
cases. Downstream delivery is drained locally afterward instead of Todoist
retrying the whole webhook. `restart-safe-ack-plan.md`'s Phase 4 (fully separate
always-on ingress process) was explicitly out of scope / not implemented —
current design keeps ingress and delivery worker in the same `proxy.py`
process, now with a periodic in-process drain loop (see uncommitted change #2
above) rather than only draining opportunistically.

## Socket activation (out of scope, 2026-06-26)

Socket activation was explored for restart safety, but the repo does not implement it.
`proxy.py` starts with the normal aiohttp host/port path; live systemd unit/socket changes
remain outside this repo's current behavior.

## SQLite connection leak (done, earlier)

`control_ledger.py:_connect` was returning a raw connection without closing it.
Fixed by converting to `@contextmanager` with explicit `conn.close()` in `finally`.
