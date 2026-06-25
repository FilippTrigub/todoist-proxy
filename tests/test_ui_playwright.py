"""DOM/SVG smoke tests for the vanilla local control UI page.

The project intentionally avoids npm, browser build tooling, and charting
libraries. These tests inspect the server-rendered HTML/SVG fixture directly so
the UI contract is covered even when Playwright is unavailable in the runtime.
"""

from __future__ import annotations

import importlib
import json
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from conftest import LOWKEYCODES_PROJECT_ID, TodoistProxyFixture


class ControlPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.main_sections: list[str] = []
        self.agent_columns: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "section" and attributes.get("data-main-section"):
            self.main_sections.append(str(attributes["data-main-section"]))
        if tag == "g" and attributes.get("class") == "agent-column":
            self.agent_columns.append(str(attributes.get("data-agent", "")))


def _module():
    return importlib.reload(importlib.import_module("control_ui"))


def _page(control_ui: Any, control_home: Path) -> str:
    response = control_ui.handle_api_request(
        "GET",
        "/index.html",
        control_home=control_home,
        token="test-token",
    )
    assert response.status == 200
    return response.body.decode("utf-8")


def _seed_ui_ledger(db_path: Path) -> None:
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
            ("item:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-older", "hash-older", "2026-06-25T09:00:00+00:00"),
            ("note:added", "proxy", LOWKEYCODES_PROJECT_ID, "smith", "task-middle", "hash-middle", "2026-06-25T09:05:00+00:00"),
            ("item:updated", "due_poller", LOWKEYCODES_PROJECT_ID, "smith", "task-newer", "hash-newer", "2026-06-25T09:10:00+00:00"),
        ]
        conn.executemany(
            """
            INSERT INTO events (event_name, source, project_id, agent, todoist_task_id, payload_hash, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            events,
        )
        interactions = [
            (1, "forward", "system", "max", "max", "forward", "exact", LOWKEYCODES_PROJECT_ID, "task-older", "hash-older", "http_200", "forwarded", "2026-06-25T09:00:01+00:00"),
            (2, "forward", "max", "smith", "smith", "forward", "inferred", LOWKEYCODES_PROJECT_ID, "task-middle", "hash-middle", "suppressed", "agent_disabled:smith", "2026-06-25T09:05:01+00:00"),
            (3, "forward", "smith", "unknown", "unknown", "forward", "inferred", LOWKEYCODES_PROJECT_ID, "task-newer", "hash-newer", "http_503", "forward_failed", "2026-06-25T09:10:01+00:00"),
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


def test_control_page_has_exact_main_sections_and_gate_controls(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    todoist_proxy_fixture.control_config_file.write_text(json.dumps({"agents": {"max": {"enabled": False}}}) + "\n")

    html = _page(control_ui, todoist_proxy_fixture.control_home)
    parser = ControlPageParser()
    parser.feed(html)

    assert parser.main_sections == ["Controls", "Timeline", "Event ledger"]
    assert "data-scope=\"global\"" in html
    assert "data-scope=\"event\"" in html
    assert "data-form=\"project\"" in html
    assert "data-scope=\"agent\"" in html
    assert 'data-scope="agent" data-name="max" data-enabled="false"' in html
    assert control_ui.TOKEN_HEADER in html
    assert "test-token" not in html


def test_timeline_svg_renders_known_agent_columns_and_fixture_arrows(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_ui_ledger(todoist_proxy_fixture.interaction_db_file)

    html = _page(control_ui, todoist_proxy_fixture.control_home)
    parser = ControlPageParser()
    parser.feed(html)

    assert parser.agent_columns == ["Max", "Abra", "Smith", "Hausmeister", "System", "Unknown"]
    arrow_matches = re.findall(r'<path class="timeline-arrow ([^"]+)"[^>]+data-event-id="(\d+)"[^>]+data-y="(\d+)"', html)
    assert len(arrow_matches) == 3
    assert {state for state, _, _ in arrow_matches} == {"forwarded", "disabled", "failed"}

    y_by_event = {event_id: int(y) for _, event_id, y in arrow_matches}
    assert y_by_event["3"] < y_by_event["1"]


def test_event_ledger_lists_events_and_routing_outcomes(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_ui_ledger(todoist_proxy_fixture.interaction_db_file)

    html = _page(control_ui, todoist_proxy_fixture.control_home)

    assert "Recent events" in html
    assert "Routing outcomes" in html
    assert "item:updated" in html
    assert "smith -> unknown" in html
    assert "forward_failed" in html
