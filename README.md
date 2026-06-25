# todoist-proxy

Routes [Todoist](https://todoist.com) webhook events to one or more downstream
HTTP services ("subscriptions"), keyed by project ID. Includes a poller that
fills the one gap Todoist webhooks don't cover: tasks whose due date/time
simply arrives without any create/update/complete action.

Built to feed [Hermes](https://github.com/FilippTrigub/hermes-agent)
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
3. Looks up which subscriptions are registered for that project in the
   routing file, and forwards the request to each of them in parallel.
4. Returns `200` to Todoist as soon as validation passes, so Todoist
   doesn't retry on routing/delivery failures — unless *every* target
   returned a 5xx, in which case it returns `502` so Todoist retries.

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

### `todoist-proxy` — control CLI

Bash wrapper for operating the proxy as a systemd user service:

```
todoist-proxy on                 # enable forwarding
todoist-proxy off                # disable forwarding (still validates HMAC, just drops)
todoist-proxy status             # show on/off state + service status
todoist-proxy restart            # restart the systemd service
todoist-proxy logs               # tail live service logs
todoist-proxy dedup-clear [id]   # clear due-poller dedup state, all rows or one task
```

The systemd unit itself isn't included here — this just controls one
assumed to be named `todoist-proxy.service`.

### `control_ui.py` — local control UI

Stdlib-only local HTTP UI and JSON API for pausing or recording Todoist to
Hermes forwarding without editing Hermes-owned files.

Run it from this repo:

```bash
python control_ui.py --port 8765
```

By default it binds to `127.0.0.1:8765`. Keep it loopback-only. V1 has no
remote auth, RBAC, or TLS, so it is not safe to expose on a public interface.
You can override the bind with `--host`, `CONTROL_UI_HOST`, or
`CONTROL_UI_PORT`, but local-only is the supported operating model.

Read-only endpoints work without a token. Writes to `POST /api/config/toggle`
require the `X-Todoist-Control-Token` header. The token comes from
`TODOIST_CONTROL_UI_TOKEN`, `TODOIST_CONTROL_UI_TOKEN_FILE`, or the generated
`control-ui-token.txt` under the control home. The API never returns the token.

The UI has three sections only: Controls, Timeline, and Event ledger. It does
not include route editing, prompt editing, replay or retry controls,
WebSockets, React, Vite, Tailwind, or any frontend build pipeline.

## Routing configuration

Both components read the same JSON file (default
`~/.hermes/todoist-routing.json`, override with `TODOIST_ROUTING_FILE`):

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

* `routes` maps a Todoist project ID to the list of subscriptions that
  should receive events for it.
* `upstreams` maps a subscription name to its base URL. Subscriptions not
  listed here default to `http://127.0.0.1:8644`.
* Events are delivered to `<upstream>/webhooks/<subscription>`.

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

* `events`: normalized inbound event metadata, task IDs, payload hashes, and
  receive times.
* `routing_decisions`: per-target forwarding decisions with config status and
  reasons.
* `interactions`: timeline rows for forwarded, suppressed, deferred, unrouted,
  and failed outcomes.
* `config_audit`: config toggle actions and config hashes.

The ledger stores SHA-256 hashes and selected metadata. It does not store raw
payload bodies, display raw secrets, or expose token values in API responses.

### Sentinel vs JSON controls

The legacy sentinel remains the emergency stop:
`~/.hermes/todoist-proxy.disabled`. If it exists, proxy forwarding is disabled
even when `todoist-control.json` would otherwise allow it. JSON controls are
the finer-grained v1 mechanism for global, event, project, agent, and combined
project/agent or agent/event gates.

When disabled, the proxy still validates Todoist HMAC signatures and records
the event or decision when the ledger is available, then suppresses delivery.
This keeps Todoist from seeing avoidable errors. For the due poller, disabled
or record-only gates record the synthetic event and suppressed routing decision
without calling subscription delivery, unblock mutation, or fired-state writes,
so a later enabled poll can retry.

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
