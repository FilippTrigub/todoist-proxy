"""SQLite interaction ledger tests for the control helper."""

from __future__ import annotations

import importlib
import sqlite3

from conftest import LOWKEYCODES_PROJECT_ID, TodoistProxyFixture


def _module():
    return importlib.reload(importlib.import_module("control_ledger"))


def _table_names(db_path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_ledger_schema_is_created_with_wal_and_busy_timeout(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)

    result = ledger.initialize_schema()

    assert result.success is True
    assert todoist_proxy_fixture.interaction_db_file.exists()
    assert {"events", "routing_decisions", "interactions", "config_audit"}.issubset(
        _table_names(todoist_proxy_fixture.interaction_db_file)
    )
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_ledger_write_helpers_store_normalized_fields_and_payload_hashes(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True

    payload = todoist_proxy_fixture.payloads["item_added"]
    event_result = ledger.record_event(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        source="proxy",
    )
    assert event_result.success is True
    assert event_result.row_id is not None
    assert event_result.payload_hash == control_ledger.payload_hash(payload["event_data"])

    decision = control_ledger.ControlDecision(
        enabled=True,
        reason="forwarding_enabled",
        source="proxy",
        event_name="item:added",
        project_id=LOWKEYCODES_PROJECT_ID,
        agent="max",
        config_status="loaded",
        config_path=str(todoist_proxy_fixture.control_config_file),
    )
    routing_result = ledger.record_routing_decision(
        decision=decision,
        target="max-lowkeycodes",
        event_row_id=event_result.row_id,
    )
    interaction_result = ledger.record_interaction(
        interaction_type="forward",
        agent="max",
        project_id=LOWKEYCODES_PROJECT_ID,
        todoist_task_id=payload["event_data"]["id"],
        status="attempted",
        payload=payload,
        event_row_id=event_result.row_id,
    )
    audit_result = ledger.record_config_audit(
        action="load",
        status="loaded",
        config_path=str(todoist_proxy_fixture.control_config_file),
        config_hash=control_ledger.payload_hash({"global": {"forwarding_enabled": True}}),
    )

    assert routing_result.success is True
    assert interaction_result.success is True
    assert audit_result.success is True

    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        event_row = conn.execute(
            "SELECT event_name, project_id, todoist_task_id, payload_hash FROM events"
        ).fetchone()
        interaction_row = conn.execute(
            "SELECT interaction_type, agent, project_id, todoist_task_id, payload_hash FROM interactions"
        ).fetchone()
        raw_payload_columns = [
            row[1]
            for table in ("events", "interactions")
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            if "payload" in row[1] and row[1] != "payload_hash"
        ]

    assert event_row == (
        "item:added",
        LOWKEYCODES_PROJECT_ID,
        payload["event_data"]["id"],
        event_result.payload_hash,
    )
    assert interaction_row == (
        "forward",
        "max",
        LOWKEYCODES_PROJECT_ID,
        payload["event_data"]["id"],
        interaction_result.payload_hash,
    )
    assert raw_payload_columns == []


def test_ledger_write_failures_are_nonfatal_return_values(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(control_ledger.sqlite3, "connect", fail_connect)

    init_result = ledger.initialize_schema()
    event_result = ledger.record_event(
        event_name="item:added",
        event_data={"id": "task-001", "project_id": LOWKEYCODES_PROJECT_ID},
    )

    assert init_result.success is False
    assert init_result.reason == "sqlite_error"
    assert "database is locked" in str(init_result.error)
    assert event_result.success is False
    assert event_result.reason == "sqlite_error"
