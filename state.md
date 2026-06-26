# Proxy State & Pending Work

## Filtering: proxy vs prompt (implemented in repo, config migration separate)

The proxy and due poller now support per-subscription route conditions in code and tests.
Legacy flat routes still work and broadcast every project event to all listed subscriptions:

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

## Socket activation (out of scope, 2026-06-26)

Socket activation was explored for restart safety, but the repo does not implement it.
`proxy.py` starts with the normal aiohttp host/port path; live systemd unit/socket changes
remain outside this repo's current behavior.

## SQLite connection leak (done, earlier)

`control_ledger.py:_connect` was returning a raw connection without closing it.
Fixed by converting to `@contextmanager` with explicit `conn.close()` in `finally`.
