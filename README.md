# todoist-proxy

Routes [Todoist](https://todoist.com) webhook events to one or more downstream
HTTP services ("subscriptions"), keyed by project ID. Includes a poller that
fills the one gap Todoist webhooks don't cover: tasks whose due date/time
simply arrives without any create/update/complete action.

Built to feed [Hermes](https://github.com/NousResearch/hermes-agent)
subscriptions, but the routing/delivery mechanism has no Hermes-specific
logic — any service that accepts `POST /webhooks/<subscription>` works.

## Components

### `proxy.py` — webhook router

An `aiohttp` server that sits in front of your Todoist OAuth app's webhook
endpoint.

1. Validates the `X-Todoist-Hmac-SHA256` header (HMAC-SHA256 over the raw
   body, keyed with the OAuth app's client secret). Requests with a missing
   or invalid signature get `401`.
2. Reads `event_data.project_id` from the payload. For `note:*` events,
   which only carry an `item_id`, it resolves the project by looking the
   task up via the Todoist API (requires `TODOIST_API_KEY`).
3. Looks up which subscriptions match the project and event in the routing
   file, durably records the inbound event and pending deliveries in SQLite,
   then returns exact HTTP `200` to Todoist.
4. Downstream delivery is drained locally after ACK. A 2xx marks a target
   successful, 3xx/4xx is terminal, and 5xx/timeouts/forwarding errors remain
   locally retryable without asking Todoist to retry the whole webhook.

The routing file is re-read on every request, so adding or changing routes
takes effect immediately without restarting the proxy.

Also serves `GET /oauth/callback`, the one-time OAuth redirect target that
exchanges an authorization code for an access token and activates webhook
delivery for an account.

### `due_poller.py` — due-task poller

Todoist only fires a webhook on create/update/complete — never when a
scheduled task's due date/time actually arrives. This script, meant to run
on a timer (e.g. a systemd timer every 10 minutes), polls the Todoist tasks
API and, for any task in a routed project that has newly become due,
synthesizes an `item:added`-shaped event and delivers it directly to that
project's subscriptions — so existing automation reacts to it exactly like
a freshly created task.

It tracks per-task dedup state in a local SQLite database so each due
occurrence fires exactly once, including for recurring tasks (which get a
new due value each time they're completed) and for recurring tasks left
incomplete past their own recurrence interval. On its first run it seeds
currently-due tasks into the dedup state without firing, so deploying it
doesn't flood subscriptions with a backlog of already-overdue tasks.
Successful due deliveries are also recorded per subscription. If one target
fails, the next poll retries only the failed target and skips targets already
recorded as successful for that task and due value.

### `todoist-proxy` — control CLI

Bash wrapper for operating the proxy as a systemd user service:

```
todoist-proxy on                 # enable forwarding
todoist-proxy off                # disable forwarding (records inbound + suppressed audit, no replay)
todoist-proxy status             # show on/off state + service status
todoist-proxy restart            # restart the systemd service
todoist-proxy logs               # tail live service logs
todoist-proxy dedup-clear [id]   # clear due-poller dedup state, all rows or one task
todoist-proxy ui --port 8765     # start the local control UI on 127.0.0.1 only
```

The systemd unit itself isn't included here — this just controls one
assumed to be named `todoist-proxy.service`.

### `control_ui.py` — local control UI

Stdlib-only local HTTP UI and JSON API for pausing or recording Todoist to
Hermes forwarding without editing Hermes-owned files.

Start it through the control CLI (preferred):

```bash
todoist-proxy ui --port 8765
```

It always binds to `127.0.0.1`; only the port is configurable with `--port` or
`CONTROL_UI_PORT`. Keep it loopback-only. V1 has no remote auth, RBAC, or TLS,
so it is not safe to expose on a public interface.

Read-only endpoints work without a token. Writes to `POST /api/config/toggle`
require the `X-Todoist-Control-Token` header. The token comes from
`TODOIST_CONTROL_UI_TOKEN`, `TODOIST_CONTROL_UI_TOKEN_FILE`, or the generated
`control-ui-token.txt` under the control home. The API never returns the token.

The UI has three sections only: Controls, Timeline, and Event ledger. It does
not include route editing, prompt editing, replay or retry controls,
WebSockets, React, Vite, Tailwind, or any frontend build pipeline.

#### Primary timeline semantics

The Timeline graph is semantic-only: it shows who triggered whom. It is not a
delivery graph and does not draw every fanout, route, suppression, retry, or
audit outcome from the ledger.

Semantic rows currently shown in the primary graph are:

* `item:added` -> `task_assigned`: task creator (`creator_uid`, falling back
  to Todoist's `added_by_uid`) to the responsible user. The Todoist task ID is
  visible in the graph and table.
* `note:added` -> `comment_mentioned`: commenter to each mentioned agent.
  Rows keep the parent task ID and comment ID metadata.
* due-poller synthetic events -> `due_triggered`: `system` to the target
  agent when a due task becomes actionable.

Delivery, fanout, routing, suppression, and config-gate audit details remain
available in the event, routing, and ledger data, but they are separate from
the primary SVG graph. Existing ledger rows are not backfilled into semantic
timeline rows; the timeline starts from the rows recorded after this behavior
is installed.

#### Delegation tree drill-down

Clicking any `task_assigned` arrow (or the `task <id>` label) in the Timeline
swaps the graph for a delegation tree: the full assign -> subtask -> assign
chain for that task, rooted at its top-most ancestor, with the clicked task
highlighted. The same task ID can be looked up directly with the search box
in the Timeline toolbar. "Back to timeline" restores the normal swim-lane
graph.

A task becomes a node in the tree from either of two already-captured
signals:

* `task_assigned` (an `item:added` with a responsible/assignee) — this is
  also the only source of the subtask link between tasks, via Todoist's own
  `parent_id`, requiring no extra API lookup.
* `comment_mentioned` (an explicit `@Name` mention in a comment on that task)
  — a handoff on the *same* task, without creating a subtask.

A task can have several handoffs over time (e.g. assigned to Max, then later
mentioned to Smith in a comment on the same task); the node shows the full
chronological sequence rather than a single owner. A task with only comment
mentions and no `task_assigned` row at all still becomes a valid node.

The tree does not infer delegation between unrelated top-level tasks with no
subtask link and no mention between them; the tree endpoint returns a 404 for
a task ID that was never seen in either signal. It also does not (yet) treat
`item:updated` reassignment as a handoff — only the assignment set at
`item:added` time is captured.

## Routing configuration

Both components read the same JSON file (default
`~/.hermes/todoist-routing.json`, override with `TODOIST_ROUTING_FILE`):

Legacy flat routes broadcast every event in the project to every listed
subscription:

```json
{
  "routes": {
    "6ggFh66x4WXVVqGH": ["hausmeister-inbox"],
    "6gmpjVFv2wVG7XJQ": ["max-lowkeycodes", "abra-lowkeycodes", "smith-lowkeycodes"]
  },
  "upstreams": {
    "hausmeister-inbox": "http://127.0.0.1:8644"
  }
}
```

Conditional routes use a per-subscription object. Missing or malformed rules
fail closed for that subscription:

```json
{
  "routes": {
    "6gmpjVFv2wVG7XJQ": {
      "max-lowkeycodes": {
        "agent": "max",
        "responsible_uids": ["59328091"],
        "creator_uids": ["59328091"],
        "section_ids": ["6gpFcCwF29V6QXxx"],
        "mention_aliases": ["@Max", "Max", "Max | CEO"]
      },
      "abra-lowkeycodes": {
        "agent": "abra",
        "responsible_uids": ["15795569"],
        "creator_uids": ["15795569"],
        "section_ids": ["6gpFcCvfqGxWcqwx"],
        "mention_aliases": ["@Abra", "Abra", "Abra | CMO"]
      },
      "smith-lowkeycodes": {
        "agent": "smith",
        "responsible_uids": ["29584133"],
        "creator_uids": ["29584133"],
        "section_ids": ["6gpFcCxmc39r8MrQ"],
        "mention_aliases": ["@Smith", "Smith", "Smith | DevOps"]
      }
    }
  },
  "upstreams": {
    "max-lowkeycodes": "http://127.0.0.1:8644",
    "abra-lowkeycodes": "http://127.0.0.1:8644",
    "smith-lowkeycodes": "http://127.0.0.1:8644"
  }
}
```

* `routes` maps a Todoist project ID to the list of subscriptions that
  should receive events for it, or to a per-subscription rule object.
* `upstreams` maps a subscription name to its base URL. Subscriptions not
  listed here default to `http://127.0.0.1:8644`.
* Events are delivered to `<upstream>/webhooks/<subscription>`.

Conditional matching rules:

* `item:added`, including due-poller synthetic `item:added`: match
  `responsible_uid` or `assignee_id` first. If no responsible or assignee is
  present, match `section_id`. Creator fields do not match this event.
* `item:updated`, `item:completed`, and `item:uncompleted`: match
  `responsible_uid` or `assignee_id` first, then unassigned `section_id`, then
  `added_by_uid`, `creator_uid`, or `creator_id` through `creator_uids`.
* `note:added`: conditional routes run in two phases. First, the proxy checks
  every configured `mention_aliases` entry as a standalone mention in the
  comment text. If any alias matches, only those explicit mention routes
  receive the event. If no alias matches, the proxy uses the parent task's
  assignment, section, and creator context. Legacy flat routes still broadcast
  `note:added`. Conditional routes fail closed when parent context cannot be
  resolved.

Route filtering only chooses delivery targets. It is not a control gate and
does not suppress an otherwise matched target. Forwarding gates still live in
`todoist-control.json` and are evaluated after route matching.

Delivery dedup is stored per target subscription:

* Webhook deliveries prefer Todoist's `X-Todoist-Delivery-ID`, which Todoist
  keeps stable across retries. If the header is missing, the proxy falls back
  to event identity plus payload hash and subscription.
* Due-poller deliveries use the task ID, due value, and subscription, so a new
  recurring due value can deliver again while retries skip successful targets.
* Success is recorded only after a downstream 2xx response. Failed targets
  remain retryable.
* Public webhook handling ACKs Todoist after durable local persistence. The
  local drain skips successful targets and retries only pending failed targets.

Out of scope for this repo: route UI, prompt edits, prompt-level dedup or
cooldown removal, replay or retry UI, WebSockets, and frontend route editing.

The Todoist webhook URL path itself doesn't affect routing — only
`project_id` does — so a single webhook registration in the Todoist app
console is enough to cover every routed project.

## Configuration

| Variable | Used by | Required | Default |
|---|---|---|---|
| `TODOIST_CLIENT_SECRET` | proxy | yes | — (process exits if unset) |
| `TODOIST_CLIENT_ID` | proxy | for `/oauth/callback` | — |
| `TODOIST_API_KEY` | proxy, poller | for `note:*` resolution / poller | — |
| `TODOIST_ROUTING_FILE` | proxy, poller | no | `~/.hermes/todoist-routing.json` |
| `TODOIST_DISABLE_FILE` | proxy | no | `~/.hermes/todoist-proxy.disabled` |
| `TODOIST_DUE_POLLER_DB` | poller | no | `~/.hermes/state/todoist_due_poller.db` |
| `TODOIST_DUE_POLLER_UNBLOCK_FILE` | poller | no | `~/.hermes/todoist-due-poller-unblock.json` |
| `PROXY_PORT` | proxy | no | `8645` |
| `CONTROL_UI_PORT` | control UI | no | `8765` |

## Local control files

`control_ui.py`, the proxy gating code, and the due poller control checks use a
dedicated UI-owned runtime home:

```text
~/todoist-hermes-control/
```

You can override it with `CONTROL_HOME`. New UI-owned files live there, not
under `~/.hermes`.

### `todoist-control.json`

Default path: `~/todoist-hermes-control/todoist-control.json`.

This file stores forwarding gates only. Missing or invalid JSON preserves the
production-compatible default: forwarding stays enabled unless the legacy
sentinel exists. Supported gate scopes are:

* `global.forwarding_enabled`
* `global.due_poller_forwarding_enabled`
* event, for example `events["item:added"]`
* project, for example `projects["<project_id>"].enabled`
* project-agent, for example `projects["<project_id>"].agents["max"]`
* agent, for example `agents["max"].enabled`
* agent-event, for example `agents["max"].events["note:added"]`

All matching gates must allow forwarding. Unspecified scopes are treated as
enabled. The Todoist route topology still lives in
`~/.hermes/todoist-routing.json`; v1 UI does not edit routes or upstreams.

### `todoist_interactions.db`

Default path: `~/todoist-hermes-control/todoist_interactions.db`.

SQLite ledger tables:

* `inbound_events`: exact authenticated inbound request bytes, allowlisted
  Todoist headers, payload hashes, and receive times for accepted webhook
  deliveries.
* `events`: normalized inbound event metadata, task IDs, payload hashes, and
  receive times.
* `routing_decisions`: per-target forwarding decisions with config status and
  reasons.
* `interactions`: semantic timeline rows plus delivery and routing audit
  outcomes. The primary UI timeline filters this table to `task_assigned`,
  `comment_mentioned`, and `due_triggered` rows.
* `config_audit`: config toggle actions and config hashes.
* `delivery_dedup`: successful delivery identities per subscription, used to
  skip already-successful targets on webhook retries and due-poller retries.

Only `inbound_events` stores raw webhook request bodies. The older audit tables
store SHA-256 hashes and selected metadata, not raw payload bodies. Ledger APIs
do not display raw secrets or expose token values in responses. Old ledger rows
are not backfilled, so historical data may exist only as audit or legacy
interaction rows.

### Sentinel vs JSON controls

The legacy sentinel remains the proxy emergency stop:
`~/.hermes/todoist-proxy.disabled`. If it exists, proxy forwarding is disabled
even when `todoist-control.json` would otherwise allow it. Due-poller
forwarding does not read this proxy sentinel; it is controlled only by JSON
gates (`global.due_poller_forwarding_enabled`, event, project, agent, and
combined project/agent or agent/event gates).

When disabled, the proxy still validates Todoist HMAC signatures, records the
inbound webhook plus suppressed audit, creates no pending deliveries, and does
not replay the suppressed event later.
For the due poller, disabled or record-only gates record the synthetic event and
suppressed routing decision without calling subscription delivery, unblock
mutation, or fired-state writes, so a later enabled poll can retry.

## Setup

1. Register a Todoist OAuth app and point its webhook + redirect URI at this
   proxy's public address (`/webhooks/<anything>` and `/oauth/callback`
   respectively).
2. `pip install aiohttp` and set `TODOIST_CLIENT_SECRET` (and optionally
   `TODOIST_CLIENT_ID`, `TODOIST_API_KEY`).
3. Create the routing file at `~/.hermes/todoist-routing.json` (or wherever
   `TODOIST_ROUTING_FILE` points).
4. Run `python3 proxy.py`, ideally under a process manager / systemd
   service named `todoist-proxy.service` so the `todoist-proxy` CLI can
   control it.
5. Visit the Todoist OAuth authorization URL once to complete the
   `/oauth/callback` exchange and activate webhook delivery.
6. Point a systemd timer (or cron) at `python3 due_poller.py` every few
   minutes to cover due-date-only events. Use `--dry-run` to preview what
   it would fire without sending anything or touching state.

## Security notes

* Every webhook request must carry a valid `X-Todoist-Hmac-SHA256`
  signature, verified with `hmac.compare_digest` against the OAuth app's
  client secret — requests are rejected before any routing or forwarding
  logic runs.
* Request bodies are capped at 1 MB.
* All secrets are read from environment variables; none are hardcoded.
* The control UI is local-only by design. Do not expose it remotely without a
  separate authenticated and TLS-protected wrapper.

## Testing

Run the full suite from this repo:

```bash
python -m pytest
python -m compileall /home/filipp/Projects/todoist-proxy
```

Useful targeted checks while working on the control UI and API:

```bash
python -m pytest tests/test_control_config.py tests/test_ledger.py tests/test_forwarding_controls.py tests/test_ui_api.py tests/test_ui_security.py tests/test_ui_playwright.py
```

## Known v1 limitations

* Local-only UI. No supported remote dashboard.
* No built-in remote auth, RBAC, or TLS.
* No prompt editor.
* No route editor. `~/.hermes/todoist-routing.json` remains the route source.
* No replay or retry UI.
* No WebSockets.
* No frontend build pipeline.

## License

MIT — see [LICENSE](LICENSE).
