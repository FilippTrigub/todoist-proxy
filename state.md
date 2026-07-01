# Proxy State & Pending Work

_Last synced with repo: 2026-07-01. Branch `main`, ahead of `origin/main`, plus uncommitted working-tree changes described below (delegation-tree drill-down)._

## Uncommitted working tree (as of this sync): delegation-tree drill-down

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
  `Session insights`, `Routing rules`).

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

Follow-up (not yet done): update README.md's token-gate description and
either delete/rewrite that test to match local-only-implies-trusted, or
remove the now-dead `_authorized`/`TOKEN_HEADER`/token-file plumbing if the
token concept is being dropped entirely.

### 5. Second, pre-existing test failure (unrelated regression, just stale)

```
tests/test_ui_playwright.py::test_control_page_has_exact_main_sections_and_gate_controls
  assert parser.main_sections == ["Controls", "Timeline", "Event ledger"]
  E   AssertionError: assert [...'Routing rules'] == [...'Event ledger']
```

Expected — the test hasn't been updated for the two new tabs added in this
diff. Needs the assertion list extended to include `Session insights` and
`Routing rules` once the tab addition is intentional/final.

## Test suite status (this sync)

```
python -m pytest -q
138 passed, 2 failed
  - test_ui_playwright.py::test_control_page_has_exact_main_sections_and_gate_controls  (stale assertion, see #5)
  - test_ui_security.py::test_post_toggle_requires_custom_token_header                  (real regression, see #4)
```

The 7 new delegation-tree tests are all in the 138 passing.

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
