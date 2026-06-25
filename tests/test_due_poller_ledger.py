"""Due-poller ledger integration and control-gate invariants."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from conftest import INBOX_PROJECT_ID, LOWKEYCODES_PROJECT_ID, TodoistProxyFixture


def _module():
    return importlib.reload(importlib.import_module("due_poller"))


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def _rows(db_path: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


def _due_task(
    *,
    task_id: str = "task-due-poll-001",
    project_id: str = INBOX_PROJECT_ID,
    responsible_uid: str | None = None,
    due: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "content": "Synthetic due poll delivery",
        "description": "",
        "project_id": project_id,
        "section_id": None,
        "parent_id": None,
        "responsible_uid": responsible_uid,
        "added_by_uid": "15611160",
        "priority": 1,
        "labels": [],
        "due": due or {"date": "2026-06-25", "string": "today"},
    }


def _bootstrap_poller(due_poller) -> None:
    conn = due_poller._connect_db()
    try:
        due_poller._mark_bootstrapped(conn)
    finally:
        conn.close()


def test_first_run_seeds_currently_due_tasks_without_delivery_or_ledger_event(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    due_poller = _module()
    task = _due_task()
    delivered: list[str] = []

    monkeypatch.setattr(sys, "argv", ["due_poller.py"])
    monkeypatch.setattr(due_poller, "_fetch_active_tasks", lambda api_key: [task])
    monkeypatch.setattr(
        due_poller,
        "_deliver",
        lambda upstream, subscription, event: delivered.append(subscription) or True,
    )

    assert due_poller.main() == 0

    assert delivered == []
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT task_id, due_value FROM fired_due") == [
        (task["id"], task["due"]["date"])
    ]
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT key, value FROM meta") == [
        ("bootstrapped", "1")
    ]
    assert _rows(todoist_proxy_fixture.interaction_db_file, "SELECT name FROM sqlite_master") == []


def test_newly_due_task_records_synthetic_event_and_system_due_interaction(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    due_poller = _module()
    _bootstrap_poller(due_poller)
    task = _due_task(
        task_id="task-lowkeycodes-001",
        project_id=LOWKEYCODES_PROJECT_ID,
        responsible_uid="29584133",
    )
    deliveries: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(sys, "argv", ["due_poller.py"])
    monkeypatch.setattr(due_poller, "_fetch_active_tasks", lambda api_key: [task])
    monkeypatch.setattr(
        due_poller,
        "_deliver",
        lambda upstream, subscription, event: deliveries.append((subscription, event)) or True,
    )

    assert due_poller.main() == 0

    assert [subscription for subscription, _ in deliveries] == [
        "max-lowkeycodes",
        "abra-lowkeycodes",
        "smith-lowkeycodes",
    ]
    assert all(event["event_data"]["_synthetic"] is True for _, event in deliveries)
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT event_name, source, project_id, todoist_task_id FROM events",
    ) == [("item:added", "due_poller", LOWKEYCODES_PROJECT_ID, task["id"])]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [
        ("abra-lowkeycodes", 1, "forwarding_enabled"),
        ("max-lowkeycodes", 1, "forwarding_enabled"),
        ("smith-lowkeycodes", 1, "forwarding_enabled"),
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        """
        SELECT actor, target, interaction_kind, confidence, status, reason
        FROM interactions ORDER BY target
        """,
    ) == [
        ("system", "abra", "due_triggered", "inferred", "http_200", "forwarded"),
        ("system", "max", "due_triggered", "inferred", "http_200", "forwarded"),
        ("system", "smith", "due_triggered", "exact", "http_200", "forwarded"),
    ]
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT task_id, due_value FROM fired_due") == [
        (task["id"], task["due"]["date"])
    ]


def test_due_poller_forwarding_disabled_records_but_does_not_post_or_mark_fired(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    _write_config(
        todoist_proxy_fixture.control_config_file,
        {"global": {"due_poller_forwarding_enabled": False}},
    )
    due_poller = _module()
    _bootstrap_poller(due_poller)
    task = _due_task(task_id="task-disabled-001")
    deliveries: list[str] = []

    monkeypatch.setattr(sys, "argv", ["due_poller.py"])
    monkeypatch.setattr(due_poller, "_fetch_active_tasks", lambda api_key: [task])
    monkeypatch.setattr(
        due_poller,
        "_deliver",
        lambda upstream, subscription, event: deliveries.append(subscription) or True,
    )

    assert due_poller.main() == 0

    assert deliveries == []
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT event_name, source, project_id, todoist_task_id FROM events",
    ) == [("item:added", "due_poller", INBOX_PROJECT_ID, task["id"])]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions",
    ) == [("hausmeister-inbox", 0, "global_due_poller_forwarding_disabled")]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT actor, target, interaction_kind, status, reason FROM interactions",
    ) == [
        (
            "system",
            "hausmeister",
            "due_triggered",
            "suppressed",
            "global_due_poller_forwarding_disabled",
        )
    ]
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT task_id, due_value FROM fired_due") == []


def test_legacy_proxy_sentinel_does_not_disable_due_poller_json_allowed_forwarding(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    _write_config(
        todoist_proxy_fixture.control_config_file,
        {"global": {"forwarding_enabled": True, "due_poller_forwarding_enabled": True}},
    )
    todoist_proxy_fixture.disable_file.touch()
    due_poller = _module()
    _bootstrap_poller(due_poller)
    task = _due_task(task_id="task-sentinel-ignored-001")
    deliveries: list[str] = []

    monkeypatch.setattr(sys, "argv", ["due_poller.py"])
    monkeypatch.setattr(due_poller, "_fetch_active_tasks", lambda api_key: [task])
    monkeypatch.setattr(
        due_poller,
        "_deliver",
        lambda upstream, subscription, event: deliveries.append(subscription) or True,
    )

    assert due_poller.main() == 0

    assert deliveries == ["hausmeister-inbox"]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions",
    ) == [("hausmeister-inbox", 1, "forwarding_enabled")]
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT task_id, due_value FROM fired_due") == [
        (task["id"], task["due"]["date"])
    ]


def test_due_poller_dedup_db_stays_independent_from_interaction_ledger(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    due_poller = _module()
    _bootstrap_poller(due_poller)
    task = _due_task(task_id="task-dedup-independent-001")

    monkeypatch.setattr(sys, "argv", ["due_poller.py"])
    monkeypatch.setattr(due_poller, "_fetch_active_tasks", lambda api_key: [task])
    monkeypatch.setattr(due_poller, "_deliver", lambda upstream, subscription, event: True)

    assert due_poller.main() == 0

    assert todoist_proxy_fixture.due_poller_db != todoist_proxy_fixture.interaction_db_file
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT name FROM sqlite_master WHERE name = 'fired_due'") == [
        ("fired_due",)
    ]
    assert _rows(
        todoist_proxy_fixture.due_poller_db,
        "SELECT name FROM sqlite_master WHERE name = 'interactions'",
    ) == []
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT name FROM sqlite_master WHERE name = 'fired_due'",
    ) == []
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT name FROM sqlite_master WHERE name = 'interactions'",
    ) == [("interactions",)]
