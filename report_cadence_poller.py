#!/usr/bin/env python3
"""
Adaptive-frequency business-state trigger for Max.

Max's CEO business-state check-in used to run on a fixed Todoist recurring
task. This poller replaces that with an adaptive interval: every run it
computes current/projected MRR from Stripe and the trailing-24h event count
from this proxy's own interaction ledger, feeds them through
``report_cadence.compute_interval_hours`` (bounded [1h, 168h]), and fires a
synthetic ``item:added``-shaped event straight to Max's downstream
subscription once that computed interval has elapsed since the last fire —
no Todoist due-date reschedule involved.

Delivery mirrors ``due_poller.py``'s already-proven mechanism: routing is
resolved in-process against ``~/.hermes/todoist-routing.json`` (the same
file the webhook proxy and due poller use), and the event is POSTed
directly to the matched subscription's upstream — proxy.py's own HTTP
ingestion path is not involved.

Required env vars: STRIPE_SECRET_KEY
Optional env vars : TODOIST_ROUTING_FILE  (default: ~/.hermes/todoist-routing.json)
                     REPORT_CADENCE_DB    (default: ~/.hermes/state/report_cadence.db)
"""
import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import report_cadence
from control_ledger import (
    ControlLedger,
    LedgerResult,
    control_config_path,
    evaluate_forwarding,
    resolve_control_home,
)
from route_matcher import match_routes

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

EVENT_NAME = "item:added"
SOURCE = "report_cadence"
REQUEST_TIMEOUT = 10

LOWKEYCODES_PROJECT_ID = "6gmpjVFv2wVG7XJQ"
MAX_UID = "59328091"
SYNTHETIC_TASK_ID = "report-cadence-max"

ROUTING_FILE = Path(
    os.environ.get("TODOIST_ROUTING_FILE", Path.home() / ".hermes" / "todoist-routing.json")
)
DB_FILE = Path(
    os.environ.get(
        "REPORT_CADENCE_DB", Path.home() / ".hermes" / "state" / "report_cadence.db"
    )
)
STATUS_META_KEY = "last_status_json"


def _load_routing() -> tuple[dict, dict[str, str]]:
    cfg = json.loads(ROUTING_FILE.read_text())
    return cfg.get("routes", {}), cfg.get("upstreams", {})


def _load_cadence_params() -> report_cadence.CadenceParams:
    """Load the live parameter overrides an operator has saved via the
    control UI's Report cadence panel (todoist-control.json's
    report_cadence key), falling back to report_cadence.py's defaults for
    anything unset or unreadable."""

    path = control_config_path(resolve_control_home())
    try:
        config = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        config = {}
    overrides = config.get(report_cadence.CADENCE_CONFIG_KEY, {})
    if not isinstance(overrides, dict):
        overrides = {}
    return report_cadence.cadence_params_from_dict(overrides)


def _connect_db() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def _get_last_fired_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_fired_at'").fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def _record_fired_at(conn: sqlite3.Connection, when: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_fired_at', ?)",
        (when.isoformat(),),
    )
    conn.commit()


def _build_status(
    *,
    status: str,
    evaluated_at: datetime,
    last_fired_at: datetime | None,
    signals: report_cadence.CadenceSignals,
    params: report_cadence.CadenceParams,
) -> dict:
    next_fire_at = None
    due = None
    if last_fired_at is not None:
        next_fire_at_dt = last_fired_at + timedelta(hours=signals.interval_hours)
        next_fire_at = next_fire_at_dt.isoformat()
        due = evaluated_at >= next_fire_at_dt
    return {
        "initialized": last_fired_at is not None,
        "source": SOURCE,
        "event_name": EVENT_NAME,
        "trigger": "report_cadence",
        "synthetic_task_id": SYNTHETIC_TASK_ID,
        "project_id": LOWKEYCODES_PROJECT_ID,
        "agent": "max",
        "status": status,
        "last_evaluated_at": evaluated_at.isoformat(),
        "last_fired_at": last_fired_at.isoformat() if last_fired_at else None,
        "interval_hours": signals.interval_hours,
        "next_fire_at": next_fire_at,
        "due": due,
        "signals": {
            "mrr_current": signals.mrr_current,
            "mrr_projected": signals.mrr_projected,
            "events_24h": signals.events_24h,
            "gap": signals.gap,
            "shortfall": signals.shortfall,
            "stagnation": signals.stagnation,
            "pressure": signals.pressure,
            "interval_hours": signals.interval_hours,
        },
        "params": report_cadence.cadence_params_to_dict(params),
    }


def _record_status(conn: sqlite3.Connection, status: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (STATUS_META_KEY, json.dumps(status, sort_keys=True, separators=(",", ":"))),
    )
    conn.commit()


def _get_status(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (STATUS_META_KEY,)).fetchone()
    if not row:
        return None
    try:
        status = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    return status if isinstance(status, dict) else None


def _build_event(content: str, description: str) -> dict:
    return {
        "event_name": EVENT_NAME,
        "event_data": {
            "id": SYNTHETIC_TASK_ID,
            "content": content,
            "description": description,
            "project_id": LOWKEYCODES_PROJECT_ID,
            "section_id": None,
            "parent_id": None,
            "responsible_uid": MAX_UID,
            "creator_uid": MAX_UID,
            "priority": 4,
            "labels": ["RECURRING"],
            "due": None,
            "_synthetic": True,
            "_trigger": "report_cadence",
        },
    }


def _log_ledger_failure(action: str, result: LedgerResult) -> None:
    if not result.success:
        log.warning("ledger %s failed: %s %s", action, result.reason, result.error or "")


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

    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        log.error("STRIPE_SECRET_KEY is not set")
        return 1

    routes, upstreams = _load_routing()
    params = _load_cadence_params()
    param_error = report_cadence.validate_cadence_params(params)
    if param_error:
        log.error("stored report_cadence params are invalid (%s) — using defaults", param_error)
        params = report_cadence.CadenceParams()

    try:
        mrr_current, mrr_projected = report_cadence.fetch_mrr_signals(stripe_key, params=params)
    except Exception as exc:
        log.error("failed to fetch Stripe MRR signals: %s", exc)
        return 1

    since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    events_24h = ControlLedger().count_events_since(since_iso)

    signals = report_cadence.compute_interval_hours(mrr_current, mrr_projected, events_24h, params=params)
    log.info(
        "signals: mrr=%.2f projected=%.2f events_24h=%d "
        "pressure=%.3f (gap=%.2f shortfall=%.2f stagnation=%.2f) interval=%.2fh "
        "[target=%.0f baseline=%.0f bounds=%.1f-%.1fh speed=%.2fx]",
        signals.mrr_current, signals.mrr_projected, signals.events_24h,
        signals.pressure, signals.gap, signals.shortfall, signals.stagnation,
        signals.interval_hours,
        params.mrr_target_eur, params.events_baseline_24h, params.t_min_hours, params.t_max_hours,
        params.speed_multiplier,
    )

    conn = _connect_db()
    try:
        now = datetime.now(timezone.utc)
        last_fired_at = _get_last_fired_at(conn)

        if last_fired_at is None:
            log.info(
                "first run — seeding last_fired_at without firing%s",
                " [dry-run]" if dry_run else "",
            )
            if not dry_run:
                _record_fired_at(conn, now)
                _record_status(
                    conn,
                    _build_status(
                        status="scheduled",
                        evaluated_at=now,
                        last_fired_at=now,
                        signals=signals,
                        params=params,
                    ),
                )
            return 0

        due_at = last_fired_at + timedelta(hours=signals.interval_hours)
        if now < due_at:
            if not dry_run:
                _record_status(
                    conn,
                    _build_status(
                        status="scheduled",
                        evaluated_at=now,
                        last_fired_at=last_fired_at,
                        signals=signals,
                        params=params,
                    ),
                )
            log.info(
                "not due yet — next fire at %s (in %.1fh)",
                due_at.isoformat(), (due_at - now).total_seconds() / 3600,
            )
            return 0

        log.info("due — firing report-cadence event%s", " [dry-run]" if dry_run else "")
        if dry_run:
            return 0

        _record_status(
            conn,
            _build_status(
                status="due",
                evaluated_at=now,
                last_fired_at=last_fired_at,
                signals=signals,
                params=params,
            ),
        )

        description = report_cadence.compose_prompt(signals, params=params)
        event = _build_event("LowKeyCodes adaptive business-state check-in", description)
        event_data = event["event_data"]

        matches = match_routes(routes, EVENT_NAME, event_data)
        if not matches:
            log.error("no matched route for report-cadence event — check %s", ROUTING_FILE)
            return 1

        ledger = ControlLedger()
        _log_ledger_failure("initialize_schema", ledger.initialize_schema())
        event_result = ledger.record_event(
            event_name=EVENT_NAME, event_data=event_data, source=SOURCE, agent="max"
        )
        _log_ledger_failure("record_event", event_result)
        event_row_id = event_result.row_id if event_result.success else None

        all_enabled_targets_successful = True
        any_enabled = False
        for match in matches:
            sub = match.subscription
            agent = match.agent or "max"
            decision = evaluate_forwarding(
                event_name=EVENT_NAME,
                project_id=LOWKEYCODES_PROJECT_ID,
                agent=agent,
                source=SOURCE,
            )
            _log_ledger_failure(
                "record_routing_decision",
                ledger.record_routing_decision(
                    decision=decision, target=sub, event_row_id=event_row_id
                ),
            )
            if not decision.enabled:
                log.info("forwarding suppressed for %s (%s)", sub, decision.reason)
                continue

            upstream = upstreams.get(sub)
            if not upstream:
                log.error("no upstream configured for %s — check %s", sub, ROUTING_FILE)
                all_enabled_targets_successful = False
                continue

            any_enabled = True
            ok = _deliver(upstream, sub, event)
            all_enabled_targets_successful = all_enabled_targets_successful and ok
            _log_ledger_failure(
                "record_interaction",
                ledger.record_interaction(
                    interaction_type="report_cadence_triggered",
                    actor="system",
                    agent=agent,
                    target=agent,
                    interaction_kind="report_cadence_triggered",
                    confidence="exact",
                    project_id=LOWKEYCODES_PROJECT_ID,
                    todoist_task_id=SYNTHETIC_TASK_ID,
                    status="http_200" if ok else "delivery_failed",
                    reason="forwarded" if ok else "forward_failed",
                    payload=event_data,
                    event_row_id=event_row_id,
                ),
            )

        if any_enabled and all_enabled_targets_successful:
            _record_fired_at(conn, now)
            _record_status(
                conn,
                _build_status(
                    status="fired",
                    evaluated_at=now,
                    last_fired_at=now,
                    signals=signals,
                    params=params,
                ),
            )
            log.info("fired — next occurrence in >= %.1fh (re-evaluated each poll)", signals.interval_hours)
            return 0

        if not any_enabled:
            _record_status(
                conn,
                _build_status(
                    status="suppressed",
                    evaluated_at=now,
                    last_fired_at=last_fired_at,
                    signals=signals,
                    params=params,
                ),
            )
            log.info("spark suppressed by forwarding gates — leaving last_fired_at unchanged")
            return 0

        _record_status(
            conn,
            _build_status(
                status="delivery_incomplete",
                evaluated_at=now,
                last_fired_at=last_fired_at,
                signals=signals,
                params=params,
            ),
        )
        log.error("delivery incomplete — will retry next poll without advancing last_fired_at")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
