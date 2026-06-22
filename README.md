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

## License

MIT — see [LICENSE](LICENSE).
