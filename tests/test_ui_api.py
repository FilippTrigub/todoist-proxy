"""Local control UI API behavior tests."""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from conftest import LOWKEYCODES_PROJECT_ID, TodoistProxyFixture


def _module():
    return importlib.reload(importlib.import_module("control_ui"))


def _json(response) -> Any:
    return json.loads(response.body.decode("utf-8"))


def _seed_ledger(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_name TEXT NOT NULL,
                source TEXT NOT NULL,
                project_id TEXT,
                agent TEXT,
                todoist_task_id TEXT,
                payload_hash TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE TABLE routing_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_row_id INTEGER,
                event_name TEXT NOT NULL,
                source TEXT NOT NULL,
                project_id TEXT,
                agent TEXT,
                target TEXT,
                enabled INTEGER NOT NULL,
                reason TEXT NOT NULL,
                config_status TEXT NOT NULL,
                config_path TEXT NOT NULL,
                decided_at TEXT NOT NULL
            );
            CREATE TABLE interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_row_id INTEGER,
                interaction_type TEXT NOT NULL,
                actor TEXT,
                agent TEXT,
                target TEXT,
                interaction_kind TEXT,
                confidence TEXT,
                project_id TEXT,
                todoist_task_id TEXT,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        for idx in range(15):
            conn.execute(
                """
                INSERT INTO events (event_name, source, project_id, agent, todoist_task_id, payload_hash, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("item:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", f"task-{idx}", f"hash-{idx}", f"2026-06-25T00:{idx:02d}:00+00:00"),
            )
            conn.execute(
                """
                INSERT INTO interactions (
                    event_row_id, interaction_type, actor, agent, target, interaction_kind, confidence,
                    project_id, todoist_task_id, payload_hash, status, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (idx + 1, "semantic", "Filipp", "max", "Max", "task_assigned", "exact", LOWKEYCODES_PROJECT_ID, f"task-{idx}", f"hash-{idx}", "recorded", "responsible_uid=59328091", f"2026-06-25T00:{idx:02d}:01+00:00"),
                )


def _seed_semantic_timeline_ledger(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_name TEXT NOT NULL,
                source TEXT NOT NULL,
                project_id TEXT,
                agent TEXT,
                todoist_task_id TEXT,
                payload_hash TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE TABLE interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_row_id INTEGER,
                interaction_type TEXT NOT NULL,
                actor TEXT,
                agent TEXT,
                target TEXT,
                interaction_kind TEXT,
                confidence TEXT,
                project_id TEXT,
                todoist_task_id TEXT,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        events = [
            ("item:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-filipp-max", "hash-1", "2026-06-25T10:00:00+00:00"),
            ("note:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-smith-max", "hash-2", "2026-06-25T10:01:00+00:00"),
            ("item:added", "due_poller", LOWKEYCODES_PROJECT_ID, "max", "task-due-max", "hash-3", "2026-06-25T10:02:00+00:00"),
            ("item:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-forward-audit", "hash-4", "2026-06-25T10:03:00+00:00"),
            ("item:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-blank-actor", "hash-5", "2026-06-25T10:04:00+00:00"),
            ("item:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-blank-target", "hash-6", "2026-06-25T10:05:00+00:00"),
        ]
        conn.executemany(
            """
            INSERT INTO events (event_name, source, project_id, agent, todoist_task_id, payload_hash, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            events,
        )
        interactions = [
            (1, "semantic", "Filipp", "max", "Max", "task_assigned", "exact", LOWKEYCODES_PROJECT_ID, "task-filipp-max", "hash-1", "recorded", "responsible_uid=59328091", "2026-06-25T10:00:01+00:00"),
            (2, "semantic", "Smith", "max", "Max", "comment_mentioned", "exact", LOWKEYCODES_PROJECT_ID, "task-smith-max", "hash-2", "recorded", "mention=@Max comment_id=comment-001", "2026-06-25T10:01:01+00:00"),
            (3, "forward", "system", "max", "Max", "due_triggered", "exact", LOWKEYCODES_PROJECT_ID, "task-due-max", "hash-3", "http_200", "forwarded", "2026-06-25T10:02:01+00:00"),
            (4, "forward", "system", "max", "max", "forward", "exact", LOWKEYCODES_PROJECT_ID, "task-forward-audit", "hash-4", "http_200", "forwarded", "2026-06-25T10:03:01+00:00"),
            (5, "semantic", "", "max", "Max", "task_assigned", "unknown_uid", LOWKEYCODES_PROJECT_ID, "task-blank-actor", "hash-5", "recorded", "missing actor", "2026-06-25T10:04:01+00:00"),
            (6, "semantic", "Filipp", "max", None, "task_assigned", "unknown_uid", LOWKEYCODES_PROJECT_ID, "task-blank-target", "hash-6", "recorded", "missing target", "2026-06-25T10:05:01+00:00"),
        ]
        conn.executemany(
            """
            INSERT INTO interactions (
                event_row_id, interaction_type, actor, agent, target, interaction_kind, confidence,
                project_id, todoist_task_id, payload_hash, status, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            interactions,
        )


def test_default_bind_host_is_loopback_and_port_is_env_configurable(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTROL_UI_HOST", "0.0.0.0")
    monkeypatch.setenv("CONTROL_UI_PORT", "9876")
    control_ui = _module()

    args = control_ui._parse_args([])

    assert control_ui.DEFAULT_HOST == "127.0.0.1"
    assert args.host == "127.0.0.1"
    assert args.port == 9876


def test_host_cli_override_is_not_accepted(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()

    try:
        control_ui._parse_args(["--host", "0.0.0.0"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - documents the required argparse failure
        raise AssertionError("--host must not be accepted for the local-only v1 UI")


def test_status_returns_redacted_control_and_ledger_summary(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_ledger(todoist_proxy_fixture.interaction_db_file)

    response = control_ui.handle_api_request(
        "GET",
        "/api/status",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    data = _json(response)

    assert response.status == 200
    assert data["server"]["host"] == "127.0.0.1"
    assert data["server"]["cors"] == "closed"
    assert data["control"]["config_status"] == "loaded"
    assert data["ledger"]["events"] == 15
    assert data["ledger"]["interactions"] == 15
    assert "test-token" not in response.body.decode("utf-8")
    assert "TODOIST_CONTROL_UI_TOKEN" not in response.body.decode("utf-8")


def test_effective_config_redacts_secret_like_keys(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.control_config_file.write_text(
        json.dumps(
            {
                "global": {"forwarding_enabled": True},
                "agents": {"max": {"enabled": True, "api_token": "secret-value"}},
                "events": {"item:added": False},
            }
        )
    )
    control_ui = _module()

    response = control_ui.handle_api_request(
        "GET",
        "/api/config/effective",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    body_text = response.body.decode("utf-8")
    data = _json(response)

    assert response.status == 200
    assert data["gates"]["global"]["forwarding_enabled"] is True
    assert data["gates"]["events"]["item:added"] is False
    assert data["redacted"] is True
    assert "secret-value" not in body_text


def test_events_and_timeline_apply_bounded_limits(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_ledger(todoist_proxy_fixture.interaction_db_file)

    events_response = control_ui.handle_api_request(
        "GET",
        "/api/events?limit=10",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    timeline_response = control_ui.handle_api_request(
        "GET",
        "/api/timeline?limit=1000",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    events = _json(events_response)
    timeline = _json(timeline_response)

    assert events_response.status == 200
    assert events["limit"] == 10
    assert len(events["events"]) == 10
    assert events["events"][0] == {
        "id": 15,
        "event_name": "item:added",
        "source": "proxy",
        "project_id": LOWKEYCODES_PROJECT_ID,
        "agent": "max",
        "todoist_task_id": "task-14",
        "payload_hash": "hash-14",
        "received_at": "2026-06-25T00:14:00+00:00",
    }
    assert timeline_response.status == 200
    assert timeline["limit"] == 100
    assert len(timeline["timeline"]) == 15
    assert {"occurred_at", "actor", "target", "interaction_kind", "confidence", "event_id", "todoist_task_id"}.issubset(
        timeline["timeline"][0]
    )


def test_timeline_api_returns_semantic_rows_only_with_todoist_task_ids(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_semantic_timeline_ledger(todoist_proxy_fixture.interaction_db_file)

    response = control_ui.handle_api_request(
        "GET",
        "/api/timeline?limit=25",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    data = _json(response)
    rows = data["timeline"]

    assert response.status == 200
    assert [row["interaction_kind"] for row in rows] == [
        "due_triggered",
        "comment_mentioned",
        "task_assigned",
    ]
    assert {row["todoist_task_id"] for row in rows} == {
        "task-filipp-max",
        "task-smith-max",
        "task-due-max",
    }
    assert all(row["interaction_kind"] != "forward" for row in rows)
    assert all(row["todoist_task_id"] != "task-forward-audit" for row in rows)
    assert all(row["todoist_task_id"] != "task-blank-actor" for row in rows)
    assert all(row["todoist_task_id"] != "task-blank-target" for row in rows)


def _seed_delegation_chain_ledger(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_row_id INTEGER,
                interaction_type TEXT NOT NULL,
                actor TEXT,
                agent TEXT,
                target TEXT,
                interaction_kind TEXT,
                confidence TEXT,
                project_id TEXT,
                todoist_task_id TEXT,
                parent_task_id TEXT,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        rows = [
            ("semantic", "Filipp", "max", "Max", "task_assigned", "exact", LOWKEYCODES_PROJECT_ID, "task-root", "", "hash-root", "recorded", "responsible_uid=59328091", "2026-06-25T10:00:00+00:00"),
            ("semantic", "Max", "smith", "Smith", "task_assigned", "exact", LOWKEYCODES_PROJECT_ID, "task-child", "task-root", "hash-child", "recorded", "responsible_uid=29584133", "2026-06-25T10:05:00+00:00"),
            ("semantic", "Smith", "abra", "Abra", "task_assigned", "exact", LOWKEYCODES_PROJECT_ID, "task-grandchild", "task-child", "hash-grandchild", "recorded", "responsible_uid=15795569", "2026-06-25T10:10:00+00:00"),
            ("semantic", "Smith", "max", "Max", "comment_mentioned", "exact", LOWKEYCODES_PROJECT_ID, "task-child", "task-root", "hash-comment", "recorded", "mention=@Max comment_id=comment-001", "2026-06-25T10:06:00+00:00"),
        ]
        conn.executemany(
            """
            INSERT INTO interactions (
                interaction_type, actor, agent, target, interaction_kind, confidence,
                project_id, todoist_task_id, parent_task_id, payload_hash, status, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def test_task_tree_builds_full_chain_from_a_mid_chain_focus_task(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_delegation_chain_ledger(todoist_proxy_fixture.interaction_db_file)

    response = control_ui.handle_api_request(
        "GET",
        "/api/task-tree?task_id=task-child",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    data = _json(response)

    assert response.status == 200
    root = data["tree"]
    assert root["task_id"] == "task-root"
    assert root["actor"] == "Filipp"
    assert root["target"] == "Max"
    assert root["is_focus"] is False
    assert len(root["children"]) == 1

    child = root["children"][0]
    assert child["task_id"] == "task-child"
    assert child["actor"] == "Max"
    assert child["target"] == "Smith"
    assert child["is_focus"] is True
    assert len(child["children"]) == 1

    grandchild = child["children"][0]
    assert grandchild["task_id"] == "task-grandchild"
    assert grandchild["actor"] == "Smith"
    assert grandchild["target"] == "Abra"
    assert grandchild["is_focus"] is False
    assert grandchild["children"] == []


def test_task_tree_unknown_task_id_returns_404(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_delegation_chain_ledger(todoist_proxy_fixture.interaction_db_file)

    response = control_ui.handle_api_request(
        "GET",
        "/api/task-tree?task_id=task-never-assigned",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )

    assert response.status == 404
    assert _json(response)["task_id"] == "task-never-assigned"


def test_task_tree_missing_task_id_param_returns_400(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_delegation_chain_ledger(todoist_proxy_fixture.interaction_db_file)

    response = control_ui.handle_api_request(
        "GET",
        "/api/task-tree",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )

    assert response.status == 400


def test_control_page_renders_with_legacy_and_blank_timeline_rows_excluded(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_semantic_timeline_ledger(todoist_proxy_fixture.interaction_db_file)

    response = control_ui.handle_api_request(
        "GET",
        "/index.html",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    body = response.body.decode("utf-8")

    assert response.status == 200
    assert "Todoist Hermes Control" in body
    assert 'data-event-id="4"' not in body
    assert "task-forward-audit" not in body
    assert "task-blank-actor" not in body
    assert "task-blank-target" not in body
    assert "task-filipp-max" in body
    assert "task-smith-max" in body
    assert "task-due-max" in body


def test_config_toggle_updates_supported_gate_with_token(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    body = json.dumps(
        {"scope": "global", "key": "forwarding_enabled", "enabled": False}
    ).encode("utf-8")

    response = control_ui.handle_api_request(
        "POST",
        "/api/config/toggle",
        body=body,
        headers={control_ui.TOKEN_HEADER: "test-token"},
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    data = _json(response)
    config = json.loads(todoist_proxy_fixture.control_config_file.read_text())

    assert response.status == 200
    assert data["ok"] is True
    assert config["global"]["forwarding_enabled"] is False


def test_serves_only_known_embedded_assets(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()

    index = control_ui.handle_api_request(
        "GET",
        "/index.html",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    arbitrary = control_ui.handle_api_request(
        "GET",
        "/../../../../etc/passwd",
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )

    assert index.status == 200
    assert b"Todoist Hermes Control" in index.body
    assert b"Controls" in index.body
    assert arbitrary.status == 404
