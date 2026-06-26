"""Due-poller ledger integration and control-gate invariants."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from conftest import INBOX_PROJECT_ID, LOWKEYCODES_PROJECT_ID, TodoistProxyFixture

SECTION_MAX = "6gpFcCwF29V6QXxx"
SECTION_ABRA = "6gpFcCvfqGxWcqwx"
SECTION_SMITH = "6gpFcCxmc39r8MrQ"


def _module():
    return importlib.reload(importlib.import_module("due_poller"))


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def _rows(db_path: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


def _write_conditional_lowkeycodes_routing(todoist_proxy_fixture: TodoistProxyFixture) -> None:
    config = json.loads(todoist_proxy_fixture.routing_file.read_text())
    config["routes"][LOWKEYCODES_PROJECT_ID] = {
        "max-lowkeycodes": {
            "agent": "max",
            "responsible_uids": ["59328091"],
            "section_ids": [SECTION_MAX],
        },
        "abra-lowkeycodes": {
            "agent": "abra",
            "responsible_uids": ["15795569"],
            "section_ids": [SECTION_ABRA],
        },
        "smith-lowkeycodes": {
            "agent": "smith",
            "responsible_uids": ["29584133"],
            "section_ids": [SECTION_SMITH],
        },
    }
    _write_config(todoist_proxy_fixture.routing_file, config)


def _due_task(
    *,
    task_id: str = "task-due-poll-001",
    project_id: str = INBOX_PROJECT_ID,
    responsible_uid: str | None = None,
    section_id: str | None = None,
    due: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "content": "Synthetic due poll delivery",
        "description": "",
        "project_id": project_id,
        "section_id": section_id,
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


def test_legacy_due_route_broadcasts_and_records_synthetic_event(
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
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        """
        SELECT source, event_name, entity_id, due_value, subscription
        FROM delivery_dedup ORDER BY subscription
        """,
    ) == [
        ("due_poller", "item:added", task["id"], task["due"]["date"], "abra-lowkeycodes"),
        ("due_poller", "item:added", task["id"], task["due"]["date"], "max-lowkeycodes"),
        ("due_poller", "item:added", task["id"], task["due"]["date"], "smith-lowkeycodes"),
    ]


def test_conditional_due_route_sends_only_responsible_owner(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    _write_conditional_lowkeycodes_routing(todoist_proxy_fixture)
    due_poller = _module()
    _bootstrap_poller(due_poller)
    task = _due_task(
        task_id="task-smith-responsible-001",
        project_id=LOWKEYCODES_PROJECT_ID,
        responsible_uid="29584133",
        section_id=SECTION_ABRA,
    )
    deliveries: list[str] = []

    monkeypatch.setattr(sys, "argv", ["due_poller.py"])
    monkeypatch.setattr(due_poller, "_fetch_active_tasks", lambda api_key: [task])
    monkeypatch.setattr(
        due_poller,
        "_deliver",
        lambda upstream, subscription, event: deliveries.append(subscription) or True,
    )

    assert due_poller.main() == 0

    assert deliveries == ["smith-lowkeycodes"]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions",
    ) == [("smith-lowkeycodes", 1, "forwarding_enabled")]
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT task_id, due_value FROM fired_due") == [
        (task["id"], task["due"]["date"])
    ]


def test_conditional_due_route_uses_unassigned_section_fallback(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    _write_conditional_lowkeycodes_routing(todoist_proxy_fixture)
    due_poller = _module()
    _bootstrap_poller(due_poller)
    task = _due_task(
        task_id="task-abra-section-001",
        project_id=LOWKEYCODES_PROJECT_ID,
        responsible_uid=None,
        section_id=SECTION_ABRA,
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

    assert [subscription for subscription, _ in deliveries] == ["abra-lowkeycodes"]
    assert deliveries[0][1]["event_data"]["section_id"] == SECTION_ABRA
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions",
    ) == [("abra-lowkeycodes", 1, "forwarding_enabled")]


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


def test_dry_run_does_not_deliver_unblock_or_mutate_ledgers(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    due_poller = _module()
    _bootstrap_poller(due_poller)
    task = _due_task(task_id="task-dry-run-001")
    deliveries: list[str] = []
    unblocked: list[str] = []

    monkeypatch.setattr(sys, "argv", ["due_poller.py", "--dry-run"])
    monkeypatch.setattr(due_poller, "_fetch_active_tasks", lambda api_key: [task])
    monkeypatch.setattr(
        due_poller,
        "_deliver",
        lambda upstream, subscription, event: deliveries.append(subscription) or True,
    )
    monkeypatch.setattr(
        due_poller,
        "_unblock",
        lambda subscription, task_id, unblock_cfg: unblocked.append(subscription),
    )

    assert due_poller.main() == 0

    assert deliveries == []
    assert unblocked == []
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT task_id, due_value FROM fired_due") == []
    assert _rows(todoist_proxy_fixture.interaction_db_file, "SELECT name FROM sqlite_master") == []


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


def test_partial_failure_retry_sends_only_targets_without_successful_delivery(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    due_poller = _module()
    _bootstrap_poller(due_poller)
    task = _due_task(task_id="task-partial-retry-001", project_id=LOWKEYCODES_PROJECT_ID)
    deliveries: list[str] = []
    unblocked: list[str] = []
    attempts: dict[str, int] = {}

    def deliver(upstream: str, subscription: str, event: dict[str, Any]) -> bool:
        deliveries.append(subscription)
        attempts[subscription] = attempts.get(subscription, 0) + 1
        return not (subscription == "abra-lowkeycodes" and attempts[subscription] == 1)

    monkeypatch.setattr(sys, "argv", ["due_poller.py"])
    monkeypatch.setattr(due_poller, "_fetch_active_tasks", lambda api_key: [task])
    monkeypatch.setattr(due_poller, "_deliver", deliver)
    monkeypatch.setattr(
        due_poller,
        "_unblock",
        lambda subscription, task_id, unblock_cfg: unblocked.append(subscription),
    )

    assert due_poller.main() == 0

    assert deliveries == ["max-lowkeycodes", "abra-lowkeycodes", "smith-lowkeycodes"]
    assert unblocked == ["max-lowkeycodes", "abra-lowkeycodes", "smith-lowkeycodes"]
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT task_id, due_value FROM fired_due") == []
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription FROM delivery_dedup ORDER BY subscription",
    ) == [("max-lowkeycodes",), ("smith-lowkeycodes",)]

    assert due_poller.main() == 0

    assert deliveries == [
        "max-lowkeycodes",
        "abra-lowkeycodes",
        "smith-lowkeycodes",
        "abra-lowkeycodes",
    ]
    assert unblocked == [
        "max-lowkeycodes",
        "abra-lowkeycodes",
        "smith-lowkeycodes",
        "abra-lowkeycodes",
    ]
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT task_id, due_value FROM fired_due") == [
        (task["id"], task["due"]["date"])
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription FROM delivery_dedup ORDER BY subscription",
    ) == [("abra-lowkeycodes",), ("max-lowkeycodes",), ("smith-lowkeycodes",)]


def test_new_due_value_permits_new_successful_delivery_for_same_task(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    due_poller = _module()
    _bootstrap_poller(due_poller)
    task = _due_task(
        task_id="task-new-due-value-001",
        due={"date": "2000-01-01", "string": "Jan 1 2000"},
    )
    deliveries: list[tuple[str, str]] = []

    monkeypatch.setattr(sys, "argv", ["due_poller.py"])
    monkeypatch.setattr(due_poller, "_fetch_active_tasks", lambda api_key: [task])
    monkeypatch.setattr(
        due_poller,
        "_deliver",
        lambda upstream, subscription, event: deliveries.append(
            (subscription, event["event_data"]["due"]["date"])
        )
        or True,
    )

    assert due_poller.main() == 0

    task["due"] = {"date": "2000-01-02", "string": "Jan 2 2000"}
    assert due_poller.main() == 0

    assert deliveries == [("hausmeister-inbox", "2000-01-01"), ("hausmeister-inbox", "2000-01-02")]
    assert _rows(todoist_proxy_fixture.due_poller_db, "SELECT task_id, due_value FROM fired_due") == [
        (task["id"], "2000-01-02")
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT due_value, subscription FROM delivery_dedup ORDER BY due_value",
    ) == [("2000-01-01", "hausmeister-inbox"), ("2000-01-02", "hausmeister-inbox")]


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
