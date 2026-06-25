#!/usr/bin/env python3
"""
Shared due-date evaluation for the Todoist proxy and due poller.

Both proxy.py (defers item:added until a future due moment arrives) and
due_poller.py (fires when that moment arrives) need to agree on exactly
what "due" means for a Todoist `due` object — date-only vs datetime,
explicit timezone vs floating local time. Keeping that logic in one place
means they can't drift out of sync with each other.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo


def due_status(due: dict, now: datetime, today: date) -> tuple[bool, str]:
    """Returns (is_due_now, due_value) for a Todoist `due` object.

    due_value is the raw due date string — used by callers that need a
    dedup/change key (e.g. detecting a recurring task's rollover).
    """
    date_str = due["date"]
    if "T" not in date_str:
        return today >= date.fromisoformat(date_str), date_str

    naive = datetime.fromisoformat(date_str)
    tz_name = due.get("timezone")
    if tz_name:
        zone = ZoneInfo(tz_name)
        is_due = datetime.now(zone) >= naive.replace(tzinfo=zone)
    else:
        # Floating time (no fixed zone) — interpret in the local system
        # timezone, matching how Todoist evaluates it for this account.
        is_due = now >= naive
    return is_due, date_str
