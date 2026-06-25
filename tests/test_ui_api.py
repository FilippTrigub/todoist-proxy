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
                (idx + 1, "forward", "system", "max", "max", "forward", "exact", LOWKEYCODES_PROJECT_ID, f"task-{idx}", f"hash-{idx}", "http_200", "forwarded", f"2026-06-25T00:{idx:02d}:01+00:00"),
            )


def test_default_bind_host_is_loopback_and_port_is_env_configurable(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTROL_UI_PORT", "9876")
    control_ui = _module()

    args = control_ui._parse_args([])

    assert control_ui.DEFAULT_HOST == "127.0.0.1"
    assert args.host == "127.0.0.1"
    assert args.port == 9876


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
    assert timeline_response.status == 200
    assert timeline["limit"] == 100
    assert len(timeline["timeline"]) == 15
    assert {"occurred_at", "actor", "target", "interaction_kind", "confidence", "event_id"}.issubset(
        timeline["timeline"][0]
    )


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
    assert b"Todoist Hermes Control API" in index.body
    assert arbitrary.status == 404
