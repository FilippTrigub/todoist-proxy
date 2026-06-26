"""SQLite interaction ledger tests for the control helper."""

from __future__ import annotations

import copy
import importlib
import json
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
    assert {
        "events",
        "routing_decisions",
        "interactions",
        "config_audit",
        "delivery_dedup",
        "inbound_events",
        "pending_deliveries",
    }.issubset(_table_names(todoist_proxy_fixture.interaction_db_file))
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_pending_deliveries_schema_matches_plan(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True

    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(pending_deliveries)").fetchall()
        ]
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(pending_deliveries)").fetchall()
        }
        foreign_keys = conn.execute("PRAGMA foreign_key_list(pending_deliveries)").fetchall()
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pending_deliveries'"
        ).fetchone()[0]

    assert columns == [
        "id",
        "inbound_event_id",
        "kind",
        "subscription",
        "state",
        "attempt_count",
        "next_attempt_at",
        "last_error",
        "created_at",
        "updated_at",
    ]
    assert {
        "pending_deliveries_delivery_idx",
        "pending_deliveries_routing_resolution_idx",
        "pending_deliveries_due_idx",
    }.issubset(indexes)
    assert any(row[2] == "inbound_events" and row[3] == "inbound_event_id" for row in foreign_keys)
    assert "kind IN ('delivery', 'routing_resolution')" in table_sql
    assert "state IN ('pending', 'retry', 'succeeded', 'terminal_failed', 'suppressed')" in table_sql


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


def test_inbound_event_delivery_id_returns_canonical_row(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]
    raw_body = b'{"event_name":"item:added","event_data":{"id":"task-001"}}'

    first = ledger.record_inbound_event(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=raw_body,
        headers={"X-Todoist-Delivery-ID": "todoist-delivery-001"},
        source="todoist",
    )
    duplicate = ledger.record_inbound_event(
        event_name=payload["event_name"],
        event_data={**payload["event_data"], "content": "changed retry body"},
        raw_body=b'{"changed":true}',
        headers={"X-Todoist-Delivery-ID": "todoist-delivery-001"},
        source="todoist",
    )

    assert first.success is True
    assert first.reason == "ok"
    assert duplicate.success is True
    assert duplicate.reason == "already_recorded"
    assert duplicate.row_id == first.row_id
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        rows = conn.execute("SELECT id, delivery_id FROM inbound_events").fetchall()

    assert rows == [(first.row_id, "todoist-delivery-001")]


def test_inbound_event_missing_delivery_id_uses_fallback_identity(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]
    raw_body = b'{"event_name":"item:added","event_data":{"id":"task-001"}}'

    first = ledger.record_inbound_event(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=raw_body,
        headers={},
        source="todoist",
    )
    duplicate = ledger.record_inbound_event(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=raw_body,
        headers={},
        source="todoist",
    )

    assert first.success is True
    assert duplicate.success is True
    assert duplicate.reason == "already_recorded"
    assert duplicate.row_id == first.row_id
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        count = conn.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0]

    assert count == 1


def test_inbound_event_missing_delivery_id_keeps_distinct_events(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]

    first = ledger.record_inbound_event(
        event_name=payload["event_name"],
        event_data={**payload["event_data"], "id": "task-001"},
        raw_body=b'{"event_data":{"id":"task-001"}}',
        headers={},
        source="todoist",
    )
    second = ledger.record_inbound_event(
        event_name=payload["event_name"],
        event_data={**payload["event_data"], "id": "task-002"},
        raw_body=b'{"event_data":{"id":"task-002"}}',
        headers={},
        source="todoist",
    )

    assert first.success is True
    assert second.success is True
    assert second.row_id != first.row_id
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        rows = conn.execute("SELECT entity_id FROM inbound_events ORDER BY id").fetchall()

    assert rows == [("task-001",), ("task-002",)]


def test_inbound_event_stores_exact_raw_body_and_allowlisted_headers(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["note_added"]
    raw_body = b'{\n  "event_data": {"id": "note-001"}, "event_name": "note:added"\n}'

    result = ledger.record_inbound_event(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=raw_body,
        headers={
            "X-Todoist-Hmac-SHA256": "signature",
            "X-Todoist-Delivery-ID": "delivery-raw-001",
            "Content-Type": "application/json",
            "Authorization": "Bearer secret",
            "User-Agent": "Todoist-Webhooks",
        },
        source="todoist",
    )

    assert result.success is True
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        stored_raw_body, stored_headers_json, stored_hash = conn.execute(
            "SELECT raw_body, headers_json, payload_hash FROM inbound_events WHERE id = ?",
            (result.row_id,),
        ).fetchone()

    assert stored_raw_body == raw_body
    assert stored_hash == control_ledger.raw_payload_hash(raw_body)
    assert json.loads(stored_headers_json) == {
        "Content-Type": "application/json",
        "X-Todoist-Delivery-ID": "delivery-raw-001",
        "X-Todoist-Hmac-SHA256": "signature",
    }


def test_atomic_inbound_event_enqueue_pending_delivery(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]
    raw_body = b'{"event_name":"item:added","event_data":{"id":"task-001"}}'

    result = ledger.record_inbound_event_and_enqueue_pending(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=raw_body,
        headers={"X-Todoist-Delivery-ID": "delivery-queue-001"},
        kind="delivery",
        subscription="max-lowkeycodes",
        source="todoist",
        next_attempt_at="2026-06-26T00:00:00+00:00",
    )

    assert result.success is True
    assert result.reason == "ok"
    assert ledger.pending_queue_depth() == 1
    due_work = ledger.due_pending_deliveries(now="2026-06-26T00:00:01+00:00")
    assert len(due_work) == 1
    assert due_work[0].kind == "delivery"
    assert due_work[0].subscription == "max-lowkeycodes"
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        inbound_count = conn.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0]
        pending_row = conn.execute(
            """
            SELECT inbound_event_id, kind, subscription, state, attempt_count,
                   next_attempt_at, last_error
            FROM pending_deliveries
            """
        ).fetchone()

    assert inbound_count == 1
    assert pending_row == (
        due_work[0].inbound_event_id,
        "delivery",
        "max-lowkeycodes",
        "pending",
        0,
        "2026-06-26T00:00:00+00:00",
        None,
    )


def test_atomic_inbound_event_enqueue_multiple_pending_deliveries(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]

    result = ledger.record_inbound_event_and_enqueue_pending_deliveries(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=b'{"event_name":"item:added","event_data":{"id":"task-001"}}',
        headers={"X-Todoist-Delivery-ID": "delivery-fanout-001"},
        subscriptions=("max-lowkeycodes", "abra-lowkeycodes"),
        source="todoist",
        next_attempt_at="2026-06-26T00:00:00+00:00",
    )

    assert result.success is True
    assert result.reason == "ok"
    assert ledger.pending_queue_depth() == 2
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        inbound_rows = conn.execute("SELECT id FROM inbound_events").fetchall()
        pending_rows = conn.execute(
            """
            SELECT inbound_event_id, kind, subscription, state
            FROM pending_deliveries
            ORDER BY subscription
            """
        ).fetchall()

    assert inbound_rows == [(result.row_id,)]
    assert pending_rows == [
        (result.row_id, "delivery", "abra-lowkeycodes", "pending"),
        (result.row_id, "delivery", "max-lowkeycodes", "pending"),
    ]


def test_atomic_pending_insert_failure_rolls_back_inbound_event(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]

    def fail_pending_insert(*args, **kwargs):
        raise sqlite3.OperationalError("pending insert failed")

    monkeypatch.setattr(ledger, "_insert_pending_delivery", fail_pending_insert)

    result = ledger.record_inbound_event_and_enqueue_pending(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=b'{"delivery":"rollback"}',
        headers={"X-Todoist-Delivery-ID": "delivery-rollback-001"},
        kind="delivery",
        subscription="max-lowkeycodes",
        source="todoist",
    )

    assert result.success is False
    assert result.reason == "sqlite_error"
    assert "pending insert failed" in str(result.error)
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        inbound_count = conn.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0]
        pending_count = conn.execute("SELECT COUNT(*) FROM pending_deliveries").fetchone()[0]

    assert inbound_count == 0
    assert pending_count == 0


def test_atomic_multiple_pending_insert_failure_rolls_back_all_rows(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]
    real_insert_pending = ledger._insert_pending_delivery
    calls = 0

    def fail_second_pending_insert(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("second pending insert failed")
        return real_insert_pending(*args, **kwargs)

    monkeypatch.setattr(ledger, "_insert_pending_delivery", fail_second_pending_insert)

    result = ledger.record_inbound_event_and_enqueue_pending_deliveries(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=b'{"delivery":"fanout-rollback"}',
        headers={"X-Todoist-Delivery-ID": "delivery-fanout-rollback-001"},
        subscriptions=("max-lowkeycodes", "abra-lowkeycodes"),
        source="todoist",
    )

    assert result.success is False
    assert result.reason == "sqlite_error"
    assert "second pending insert failed" in str(result.error)
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        inbound_count = conn.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0]
        pending_count = conn.execute("SELECT COUNT(*) FROM pending_deliveries").fetchone()[0]

    assert inbound_count == 0
    assert pending_count == 0


def test_routing_resolution_pending_work_allows_null_subscription(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["note_added"]

    result = ledger.record_inbound_event_and_enqueue_pending(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=b'{"event_name":"note:added"}',
        headers={"X-Todoist-Delivery-ID": "delivery-routing-001"},
        kind="routing_resolution",
        source="todoist",
        next_attempt_at="2026-06-26T00:00:00+00:00",
    )

    assert result.success is True
    due_work = ledger.due_pending_deliveries(now="2026-06-26T00:00:01+00:00")
    assert [(work.kind, work.subscription) for work in due_work] == [
        ("routing_resolution", None)
    ]
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        row = conn.execute(
            "SELECT kind, subscription FROM pending_deliveries"
        ).fetchone()

    assert row == ("routing_resolution", None)


def test_duplicate_pending_delivery_work_is_idempotent(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]
    raw_body = b'{"event_name":"item:added","event_data":{"id":"task-001"}}'

    first = ledger.record_inbound_event_and_enqueue_pending(
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        raw_body=raw_body,
        headers={"X-Todoist-Delivery-ID": "delivery-duplicate-001"},
        kind="delivery",
        subscription="max-lowkeycodes",
        source="todoist",
    )
    duplicate = ledger.record_inbound_event_and_enqueue_pending(
        event_name=payload["event_name"],
        event_data={**payload["event_data"], "content": "retry body changed"},
        raw_body=b'{"changed":true}',
        headers={"X-Todoist-Delivery-ID": "delivery-duplicate-001"},
        kind="delivery",
        subscription="max-lowkeycodes",
        source="todoist",
    )

    assert first.success is True
    assert duplicate.success is True
    assert duplicate.reason == "already_pending"
    assert duplicate.row_id == first.row_id
    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        inbound_count = conn.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0]
        pending_count = conn.execute("SELECT COUNT(*) FROM pending_deliveries").fetchone()[0]

    assert inbound_count == 1
    assert pending_count == 1


def test_queue_depth_counts_pending_and_retry_only(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]
    states = ["pending", "retry", "succeeded", "terminal_failed", "suppressed"]
    for index, state in enumerate(states):
        result = ledger.record_inbound_event_and_enqueue_pending(
            event_name=payload["event_name"],
            event_data={**payload["event_data"], "id": f"task-{index}"},
            raw_body=f'{{"id":"task-{index}"}}'.encode(),
            headers={"X-Todoist-Delivery-ID": f"delivery-depth-{index}"},
            kind="delivery",
            subscription="max-lowkeycodes",
            source="todoist",
            next_attempt_at="2026-06-26T00:00:00+00:00",
        )
        assert result.success is True
        with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
            conn.execute(
                "UPDATE pending_deliveries SET state = ? WHERE id = ?",
                (state, result.row_id),
            )

    assert ledger.pending_queue_depth() == 2
    due_work = ledger.due_pending_deliveries(now="2026-06-26T00:00:01+00:00")
    assert [work.state for work in due_work] == ["pending", "retry"]


def test_delivery_dedup_records_success_per_subscription(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["item_added"]

    assert ledger.has_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="max-lowkeycodes",
    ) is False

    first_result = ledger.record_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="max-lowkeycodes",
    )
    duplicate_result = ledger.record_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="max-lowkeycodes",
    )

    assert first_result.success is True
    assert first_result.reason == "ok"
    assert duplicate_result.success is True
    assert duplicate_result.reason == "already_delivered"
    assert duplicate_result.row_id == first_result.row_id
    assert ledger.has_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="max-lowkeycodes",
    ) is True
    assert ledger.has_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="abra-lowkeycodes",
    ) is False

    with sqlite3.connect(todoist_proxy_fixture.interaction_db_file) as conn:
        rows = conn.execute(
            "SELECT source, event_name, entity_id, subscription FROM delivery_dedup"
        ).fetchall()

    assert rows == [("todoist", "item:added", payload["event_data"]["id"], "max-lowkeycodes")]


def test_delivery_dedup_keeps_due_values_retryable_per_subscription(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["due_poll_item_added"]

    result = ledger.record_successful_delivery(
        source="due_poller",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="hausmeister-inbox",
        due_value="2026-06-25",
    )

    assert result.success is True
    assert ledger.has_successful_delivery(
        source="due_poller",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="hausmeister-inbox",
        due_value="2026-06-25",
    ) is True
    assert ledger.has_successful_delivery(
        source="due_poller",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="hausmeister-inbox",
        due_value="2026-06-26",
    ) is False


def test_delivery_dedup_payload_hash_fallback_and_delivery_id_preference(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.interaction_db_file.unlink()
    control_ledger = _module()
    ledger = control_ledger.ControlLedger(control_home=todoist_proxy_fixture.control_home)
    assert ledger.initialize_schema().success is True
    payload = todoist_proxy_fixture.payloads["note_added"]
    changed_event_data = copy.deepcopy(payload["event_data"])
    changed_event_data["content"] = "@Smith changed comment body"

    fallback_result = ledger.record_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="smith-lowkeycodes",
    )

    assert fallback_result.success is True
    assert ledger.has_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=changed_event_data,
        subscription="smith-lowkeycodes",
    ) is False

    delivery_id_result = ledger.record_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="smith-lowkeycodes",
        delivery_id="todoist-delivery-001",
    )

    assert delivery_id_result.success is True
    assert ledger.has_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=changed_event_data,
        subscription="smith-lowkeycodes",
        delivery_id="todoist-delivery-001",
    ) is True


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
    delivery_result = ledger.record_successful_delivery(
        source="todoist",
        event_name="item:added",
        event_data={"id": "task-001", "project_id": LOWKEYCODES_PROJECT_ID},
        subscription="max-lowkeycodes",
    )

    assert init_result.success is False
    assert init_result.reason == "sqlite_error"
    assert "database is locked" in str(init_result.error)
    assert event_result.success is False
    assert event_result.reason == "sqlite_error"
    assert delivery_result.success is False
    assert delivery_result.reason == "sqlite_error"
