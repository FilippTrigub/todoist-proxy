#!/usr/bin/env python3
"""
Todoist due-task poller.

Todoist does not emit a webhook when a scheduled task's due date/time
arrives (only on create/update/complete). This script polls the REST API
every 10 minutes (via systemd timer) and, for any task in a routed project
whose due moment has newly arrived, synthesizes an item:added-shaped event
and delivers it directly to the Hermes subscriptions registered for that
project — so existing prompts react to it exactly like a freshly created
task.

Routing is read from the same file the webhook proxy uses
(~/.hermes/todoist-routing.json), so any project already wired for
item:added is automatically covered here too — no project IDs are
hardcoded in this script. event_data.id is always the real, untouched
Todoist task ID, so prompts that fetch/complete/comment on the task via
the Todoist API keep working unmodified.

Dedup state (~/.hermes/state/todoist_due_poller.db, sqlite) tracks, per
task ID, the due value last fired for. A recurring task gets a new due
value each time it rolls over on completion, so each occurrence fires
exactly once. On the very first run (empty db), currently-due tasks are
seeded into state without firing, to avoid flooding agents with a backlog
of already-overdue tasks at deploy time.

Todoist only rolls a recurring task's due value forward when it is marked
done — an incomplete recurring task (e.g. "every 6 hours") keeps the exact
same due value indefinitely, so the due-value dedup above would otherwise
fire it once and never again. To cover that case, the poller also tracks
how long it's been since the last fire and, once the task's own recurrence
interval has elapsed, clears its dedup row so it's treated as newly due —
see _parse_recurrence_interval / _expire_if_interval_elapsed. Only fixed
intervals parsed from due.string ("every N hours/days/weeks", "every
other ...") are supported; weekday-specific or monthly/yearly recurrences
fall back to the standard due-value-change behavior.

Use `todoist-proxy dedup-clear [task_id]` to manually clear the dedup
table (all rows, or just one task) if a task needs to be forced to fire
again immediately.

Recurring tasks keep the same task ID across occurrences, so a
subscription prompt that dedups against its own flat handled_task_ids /
seen_task_ids set would otherwise see occurrence 2+ as "already handled"
and skip it forever after the first. Rather than changing that prompt,
this script can directly clear the task ID from such a state file right
before redelivering — see TODOIST_DUE_POLLER_UNBLOCK_FILE. This edits
*data* the subscription's prompt already reads/writes itself; it does not
touch the prompt's instructions.

Required env vars: TODOIST_API_KEY
Optional env vars : TODOIST_ROUTING_FILE            (default: ~/.hermes/todoist-routing.json)
                     TODOIST_DUE_POLLER_DB           (default: ~/.hermes/state/todoist_due_poller.db)
                     TODOIST_DUE_POLLER_UNBLOCK_FILE (default: ~/.hermes/todoist-due-poller-unblock.json)
"""
import json
import logging
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from control_ledger import ControlLedger, LedgerResult, evaluate_forwarding
from due_utils import due_status
from route_matcher import match_routes

try:
    import fcntl
except ImportError:  # not available on non-POSIX platforms
    fcntl = None

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

TODOIST_TASKS_URL = "https://api.todoist.com/api/v1/tasks"
EVENT_NAME = "item:added"
REQUEST_TIMEOUT = 10

ROUTING_FILE = Path(
    os.environ.get("TODOIST_ROUTING_FILE", Path.home() / ".hermes" / "todoist-routing.json")
)
DB_FILE = Path(
    os.environ.get(
        "TODOIST_DUE_POLLER_DB", Path.home() / ".hermes" / "state" / "todoist_due_poller.db"
    )
)
UNBLOCK_FILE = Path(
    os.environ.get(
        "TODOIST_DUE_POLLER_UNBLOCK_FILE",
        Path.home() / ".hermes" / "todoist-due-poller-unblock.json",
    )
)
SUBSCRIPTION_AGENT_MAP = {
    "max-lowkeycodes": "max",
    "abra-lowkeycodes": "abra",
    "smith-lowkeycodes": "smith",
    "hausmeister-inbox": "hausmeister",
}
AGENT_UID_MAP = {
    "max": "59328091",
    "abra": "15795569",
    "smith": "29584133",
    "hausmeister": "59138424",
}


def _load_routing() -> tuple[dict[str, list[str]], dict[str, str]]:
    cfg = json.loads(ROUTING_FILE.read_text())
    return cfg.get("routes", {}), cfg.get("upstreams", {})


def _load_unblock_config() -> dict[str, dict]:
    """subscription -> {state_file, id_fields: [...]} for subscriptions whose
    own handled-state needs clearing before a recurring due-event is redelivered."""
    try:
        return json.loads(UNBLOCK_FILE.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("unblock config unreadable (%s) — skipping unblock step", exc)
        return {}


def _connect_db() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fired_due ("
        "task_id TEXT PRIMARY KEY, due_value TEXT NOT NULL, fired_at TEXT NOT NULL)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def _is_bootstrapped(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM meta WHERE key = 'bootstrapped'").fetchone()
    return row is not None


def _mark_bootstrapped(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('bootstrapped', '1')")
    conn.commit()


def _last_fired_due(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute(
        "SELECT due_value FROM fired_due WHERE task_id = ?", (task_id,)
    ).fetchone()
    return row[0] if row else None


def _record_fired(conn: sqlite3.Connection, task_id: str, due_value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fired_due (task_id, due_value, fired_at) VALUES (?, ?, ?)",
        (task_id, due_value, datetime.now().isoformat()),
    )
    conn.commit()


_HOUR_RE = re.compile(r"every\s+(?:other\s+)?(\d+)?\s*hours?\b")
_DAY_RE = re.compile(r"every\s+(?:other\s+)?(\d+)?\s*days?\b")
_WEEK_RE = re.compile(r"every\s+(?:other\s+)?(\d+)?\s*weeks?\b")


def _parse_recurrence_interval(due: dict) -> timedelta | None:
    """Best-effort fixed interval for a recurring due string, e.g. "every 6
    hours" -> timedelta(hours=6). Returns None for patterns without a fixed
    interval (specific weekdays, monthly/yearly, "every weekday", non-recurring,
    etc.) — callers fall back to the standard due_value-change dedup for those.
    """
    if not due.get("is_recurring"):
        return None
    text = (due.get("string") or "").strip().lower()
    if "weekday" in text:
        return None
    multiplier = 2 if "every other" in text else 1

    if match := _HOUR_RE.search(text):
        return timedelta(hours=int(match.group(1) or 1) * multiplier)
    if match := _DAY_RE.search(text):
        return timedelta(days=int(match.group(1) or 1) * multiplier)
    if match := _WEEK_RE.search(text):
        return timedelta(weeks=int(match.group(1) or 1) * multiplier)
    return None


def _expire_if_interval_elapsed(
    conn: sqlite3.Connection,
    task_id: str,
    due_value: str,
    due: dict,
    now: datetime,
    dry_run: bool,
) -> bool:
    """Todoist only rolls due_value forward when a recurring task is marked
    done — an incomplete recurring task keeps the same due_value forever, so
    the normal due_value-change dedup would silently never fire it again.

    If this exact due_value was already fired and the task's own recurrence
    interval has since elapsed, drop its dedup row so the caller's existing
    due_value check treats it as newly due. Returns True whenever the
    interval has elapsed (so dry-run can report it without mutating state).
    """
    row = conn.execute(
        "SELECT due_value, fired_at FROM fired_due WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None or row[0] != due_value:
        return False  # no prior fire for this exact due_value to expire

    interval = _parse_recurrence_interval(due)
    if interval is None:
        return False

    fired_at = datetime.fromisoformat(row[1])
    if now - fired_at < interval:
        return False

    log.info(
        "task %s: recurrence interval (%s) elapsed since last fire at %s with "
        "due_value unchanged (%s, Todoist hasn't rolled it over) — treating as "
        "newly due%s",
        task_id, interval, row[1], due_value, " [dry-run]" if dry_run else "",
    )
    if not dry_run:
        conn.execute("DELETE FROM fired_due WHERE task_id = ?", (task_id,))
        conn.commit()
    return True


def _fetch_active_tasks(api_key: str) -> list[dict]:
    tasks: list[dict] = []
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        url = TODOIST_TASKS_URL + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
        tasks.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return tasks


def _build_event(task: dict) -> dict:
    return {
        "event_name": EVENT_NAME,
        "event_data": {
            "id": task["id"],
            "content": task["content"],
            "description": task.get("description", ""),
            "project_id": task["project_id"],
            "section_id": task.get("section_id"),
            "parent_id": task.get("parent_id"),
            "responsible_uid": task.get("responsible_uid"),
            "creator_uid": task.get("added_by_uid"),
            "priority": task.get("priority"),
            "labels": task.get("labels", []),
            "due": task.get("due"),
            "_synthetic": True,
            "_trigger": "due_poll",
        },
    }


def _agent_for_subscription(subscription: str) -> str:
    """Map known subscription names to control-ledger agent scopes."""

    return SUBSCRIPTION_AGENT_MAP.get(subscription, "")


def _interaction_confidence(task: dict, agent: str) -> str:
    """Return exact when Todoist explicitly assigned the task to the target agent.

    Otherwise the due-trigger relationship is inferred from routing/project
    fanout. This keeps the rule simple and avoids storing raw payload details
    in the ledger.
    """

    return "exact" if task.get("responsible_uid") == AGENT_UID_MAP.get(agent) else "inferred"


def _log_ledger_failure(action: str, result: LedgerResult) -> None:
    if not result.success:
        log.warning("ledger %s failed: %s %s", action, result.reason, result.error or "")


def _todoist_task_id(event_data: dict) -> str:
    return str(event_data.get("id", ""))


def _record_due_interaction(
    ledger: ControlLedger,
    *,
    agent: str,
    task: dict,
    event_data: dict,
    status: str,
    reason: str,
    event_row_id: int | None,
) -> None:
    result = ledger.record_interaction(
        interaction_type="due_triggered",
        actor="system",
        agent=agent,
        target=agent,
        interaction_kind="due_triggered",
        confidence=_interaction_confidence(task, agent),
        project_id=str(event_data.get("project_id", "")),
        todoist_task_id=_todoist_task_id(event_data),
        status=status,
        reason=reason,
        payload=event_data,
        event_row_id=event_row_id,
    )
    _log_ledger_failure("record_due_interaction", result)


def _unblock(subscription: str, task_id: str, unblock_cfg: dict[str, dict]) -> None:
    """Remove task_id from a subscription's own handled-state arrays, if configured.

    Edits only the state *data* a subscription's prompt already reads and
    writes itself — never the prompt text. Best-effort: a lock file guards
    our own read-modify-write, but the prompt may also write this file
    concurrently outside our control, so this is not a hard guarantee
    against races, just a low-cost mitigation.
    """
    cfg = unblock_cfg.get(subscription)
    if not cfg:
        return

    state_path = Path(os.path.expanduser(cfg["state_file"]))
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_fh = open(lock_path, "w")
    try:
        if fcntl:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return

        changed = False
        for field in cfg.get("id_fields", []):
            ids = state.get(field)
            if isinstance(ids, list) and task_id in ids:
                ids.remove(task_id)
                changed = True

        if changed:
            tmp = state_path.with_suffix(state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(state_path)
            log.info("unblocked %s in %s (%s)", task_id, state_path, subscription)
    finally:
        if fcntl:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def _deliver(upstream: str, subscription: str, event: dict) -> bool:
    url = f"{upstream}/webhooks/{subscription}"
    body = json.dumps(event).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-GitHub-Event": EVENT_NAME},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            log.info("  -> %s %d", url, resp.status)
            return resp.status < 300
    except urllib.error.HTTPError as exc:
        log.error("  -> %s HTTP %d", url, exc.code)
        return False
    except Exception as exc:
        log.error("  -> %s error: %s", url, exc)
        return False


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    api_key = os.environ.get("TODOIST_API_KEY", "")
    if not api_key:
        log.error("TODOIST_API_KEY is not set")
        return 1

    routes, upstreams = _load_routing()
    watched_projects = set(routes)
    if not watched_projects:
        log.info("no routed projects in %s — nothing to poll", ROUTING_FILE)
        return 0

    unblock_cfg = _load_unblock_config()

    try:
        tasks = _fetch_active_tasks(api_key)
    except Exception as exc:
        log.error("failed to fetch tasks from Todoist: %s", exc)
        return 1

    now = datetime.now()
    today = date.today()

    due_tasks = []
    for task in tasks:
        if task.get("project_id") not in watched_projects or not task.get("due"):
            continue
        is_due, due_value = due_status(task["due"], now, today)
        if is_due:
            due_tasks.append((task, due_value))

    conn = _connect_db()
    try:
        if not _is_bootstrapped(conn):
            log.info(
                "first run — seeding %d currently-due task(s) without firing",
                len(due_tasks),
            )
            if not dry_run:
                for task, due_value in due_tasks:
                    _record_fired(conn, task["id"], due_value)
                _mark_bootstrapped(conn)
            return 0

        fired = 0
        for task, due_value in due_tasks:
            task_id = task["id"]
            interval_elapsed = _expire_if_interval_elapsed(
                conn, task_id, due_value, task["due"], now, dry_run
            )
            if not interval_elapsed and _last_fired_due(conn, task_id) == due_value:
                continue  # already notified for this occurrence

            event = _build_event(task)
            event_data = event["event_data"]
            matches = match_routes(routes, EVENT_NAME, event_data)
            subscriptions = [match.subscription for match in matches]
            log.info(
                "task %s due (%s) -> %s%s",
                task_id, due_value, ", ".join(subscriptions), " [dry-run]" if dry_run else "",
            )
            if dry_run:
                continue
            if not matches:
                log.info("task %s: no matched due route — will retry next poll", task_id)
                continue

            ledger = ControlLedger()
            _log_ledger_failure("initialize_schema", ledger.initialize_schema())
            event_result = ledger.record_event(
                event_name=EVENT_NAME,
                event_data=event_data,
                source="due_poller",
            )
            _log_ledger_failure("record_event", event_result)
            event_row_id = event_result.row_id if event_result.success else None

            enabled_targets: list[tuple[str, str, str]] = []
            for match in matches:
                sub = match.subscription
                agent = match.agent or _agent_for_subscription(sub)
                decision = evaluate_forwarding(
                    event_name=EVENT_NAME,
                    project_id=str(task.get("project_id", "")),
                    agent=agent,
                    source="due_poller",
                )
                _log_ledger_failure(
                    "record_routing_decision",
                    ledger.record_routing_decision(
                        decision=decision,
                        target=sub,
                        event_row_id=event_row_id,
                    ),
                )
                if decision.enabled:
                    enabled_targets.append((sub, upstreams.get(sub, "http://127.0.0.1:8644"), agent))
                else:
                    log.info("task %s: forwarding suppressed for %s (%s)", task_id, sub, decision.reason)
                    _record_due_interaction(
                        ledger,
                        agent=agent,
                        task=task,
                        event_data=event_data,
                        status="suppressed",
                        reason=decision.reason,
                        event_row_id=event_row_id,
                    )

            all_enabled_targets_successful = bool(enabled_targets)
            for sub, upstream, agent in enabled_targets:
                if ledger.has_successful_delivery(
                    source="due_poller",
                    event_name=EVENT_NAME,
                    event_data=event_data,
                    subscription=sub,
                    due_value=due_value,
                ):
                    log.info("task %s: %s already delivered for due %s — skipping", task_id, sub, due_value)
                    continue

                _unblock(sub, task_id, unblock_cfg)
                ok = _deliver(upstream, sub, event)
                all_enabled_targets_successful = all_enabled_targets_successful and ok
                _record_due_interaction(
                    ledger,
                    agent=agent,
                    task=task,
                    event_data=event_data,
                    status="http_200" if ok else "delivery_failed",
                    reason="forwarded" if ok else "forward_failed",
                    event_row_id=event_row_id,
                )
                if ok:
                    delivery_result = ledger.record_successful_delivery(
                        source="due_poller",
                        event_name=EVENT_NAME,
                        event_data=event_data,
                        subscription=sub,
                        due_value=due_value,
                    )
                    _log_ledger_failure("record_successful_delivery", delivery_result)
                    all_enabled_targets_successful = (
                        all_enabled_targets_successful and delivery_result.success
                    )

            if all_enabled_targets_successful:
                _record_fired(conn, task_id, due_value)
                fired += 1
            else:
                log.error("task %s: not all enabled deliveries succeeded — will retry next poll", task_id)

        log.info("poll complete — %d task(s) fired", fired)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
