# Restart-safe Todoist webhook ACK plan

## Goal

Make the public Todoist webhook endpoint always return HTTP `200` for authentic Todoist deliveries and record the event, even when agent forwarding is disabled or the forwarding worker is being restarted. This prevents Todoist from treating transient Hermes/proxy maintenance as a broken callback URL.

## Key constraint

A stopped process cannot return `200`. Restart safety cannot be solved only by changing the current `proxy.py` request handler if the whole process is down. The durable design needs a small ingress layer that stays up and ACKs/records events independently from the restart-prone forwarding work.

## Current weak spots

- `todoist-proxy off` currently prevents forwarding, but the intent should be clearer: still validate HMAC, parse payload, record the event, and return `200`.
- During service/socket mistakes, `proxy.py` can fail to start and no application code is available to ACK Todoist.
- The current handler can return `502` when downstream Hermes forwarding fails. That is useful for retries, but it also tells Todoist the callback endpoint is unhealthy.
- `todoist-proxy status` checks the service and disable sentinel only; it does not prove socket activation, public reachability, or post-restart ACK behavior.

## Recommended design: ACK-first ingress + async delivery

Split the public webhook path into two responsibilities:

1. **Ingress/ACK path** — stable, minimal, must stay running.
   - Accept `POST /webhooks/...`.
   - Read the raw body.
   - Validate `X-Todoist-Hmac-SHA256`.
   - Parse JSON enough to extract `event_name`, `event_data.id`, `event_data.project_id`, and `X-Todoist-Delivery-ID`.
   - Record the raw event and routing metadata in SQLite before returning.
   - Return `200` for every valid Todoist event, even if forwarding is disabled or downstream Hermes is unavailable.
   - Return non-200 only for unauthentic/malformed requests (`401` invalid HMAC, `400` invalid JSON/body too large).

2. **Delivery worker path** — restartable, allowed to fail/retry internally.
   - Reads pending recorded events from SQLite.
   - Applies future-due deferral, routing, control gates, delivery dedup, and downstream forwarding.
   - Records per-target delivery outcomes.
   - Retries failed downstream targets from local state instead of asking Todoist to retry the whole webhook.

This changes Todoist from the retry system of record to a notification source. The local SQLite ledger becomes the durable retry queue.

## Quick implementation phases

### Phase 1 — Make valid inbound events ACK-first inside current `proxy.py`

This is the smallest useful code change and should be done first.

- Move event recording earlier and make it unconditional after HMAC + JSON validation.
- When `~/.hermes/todoist-proxy.disabled` exists:
  - record the event with a `suppressed` / `forwarding_disabled` interaction;
  - return `200`;
  - do not forward.
- When no route matches:
  - record the event as `unrouted`;
  - return `200`.
- When a task is future-due:
  - record the event as `deferred_due_future`;
  - return `200`.
- When downstream forwarding fails:
  - record per-target failure;
  - enqueue retry locally;
  - return `200` to Todoist.

Success criteria:

- Synthetic valid HMAC payload returns `200` in all forwarding-disabled, no-route, future-due, and downstream-failure cases.
- SQLite ledger contains one inbound event row for every valid payload.
- Invalid HMAC still returns `401` and is not recorded as trusted.

### Phase 2 — Add a durable `pending_deliveries` queue

Add a small SQLite table, likely in `todoist_interactions.db`:

```sql
CREATE TABLE IF NOT EXISTS inbound_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL DEFAULT 'proxy',
  event_name TEXT NOT NULL,
  entity_id TEXT,
  project_id TEXT,
  delivery_id TEXT,
  payload_hash TEXT NOT NULL,
  raw_body BLOB NOT NULL,
  headers_json TEXT NOT NULL,
  received_at TEXT NOT NULL,
  acked_at TEXT NOT NULL,
  UNIQUE(delivery_id)
);

CREATE TABLE IF NOT EXISTS pending_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  inbound_event_id INTEGER NOT NULL,
  subscription TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(inbound_event_id, subscription)
);
```

Notes:

- If Todoist omits `X-Todoist-Delivery-ID`, dedup by `(event_name, entity_id, payload_hash)`.
- Store raw body so retries use the exact event Todoist sent.
- Keep current `delivery_dedup` for successful per-target delivery; `pending_deliveries` tracks work still owed.

### Phase 3 — Extract a restartable delivery worker

Add either:

- `delivery_worker.py` run by a systemd timer every minute; or
- an in-process background task in `proxy.py` plus a timer fallback.

Prefer a timer/oneshot worker first because it is simpler and survives proxy restarts cleanly.

Worker behavior:

- Select due `pending_deliveries` where `status IN ('pending', 'retry')` and `next_attempt_at <= now`.
- Reconstruct the payload from `inbound_events.raw_body`.
- Re-check route/control gates if needed.
- POST to the subscription upstream.
- On 2xx, mark success and update `delivery_dedup`.
- On failure, increment `attempt_count`, store error, and set exponential backoff.
- Never return errors to Todoist; all retries are local.

### Phase 4 — Optional always-on micro-ingress

If restarts must be safe even while changing/restarting the main proxy process, split the process:

- `todoist-ingress.service` owns the public socket and does only HMAC validation, SQLite insert, and `200` ACK.
- `todoist-delivery-worker.service` / timer does routing and forwarding.
- The current `todoist-proxy.service` can then be retired or become the worker.

This is the only design that truly satisfies “still returns 200 while the forwarding proxy is restarting.” Socket activation helps with short restarts, but an always-on ingress process is stronger because it can ACK immediately instead of depending on the worker startup path.

## Systemd / ops changes

- Keep `todoist-proxy.socket` continuously active; do not restart it during normal maintenance.
- Update `todoist-proxy restart` to restart only the delivery worker once Phase 4 exists.
- Expand `todoist-proxy status` to check:
  - service active;
  - socket active;
  - latest startup mode is `starting via socket activation`;
  - public signed canary POST returns `200`;
  - pending retry queue depth.
- Add `todoist-proxy canary`:
  - sends a signed diagnostic event through the public Tailscale URL;
  - verifies it appears in the inbound ledger;
  - exits non-zero if not.

## Test plan

- Unit tests:
  - valid HMAC + disabled forwarding records and returns `200`;
  - valid HMAC + downstream 500 records failure, queues retry, returns `200`;
  - valid HMAC + no route records `unrouted`, returns `200`;
  - invalid HMAC returns `401` and does not enqueue;
  - duplicate `X-Todoist-Delivery-ID` is idempotent.
- Integration tests:
  - start fake Hermes upstream returning 500, send signed payload, assert Todoist-facing response is `200` and retry row exists;
  - run worker after changing fake upstream to 202/200, assert retry clears;
  - simulate service restart and confirm socket/public canary still returns `200`.

## Open decision

Choose how much to build now:

1. **Small patch:** ACK-first behavior in current `proxy.py` plus local retry rows. Fastest, but process restarts can still interrupt ACKs.
2. **Robust patch:** separate always-on ingress from delivery worker. More work, but actually protects Todoist from forwarding-worker restarts.

Recommendation: implement Phase 1 + Phase 2 immediately, then split ingress/worker if another restart causes Todoist delivery loss.
