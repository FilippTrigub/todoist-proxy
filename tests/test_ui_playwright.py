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
            ("item:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-filipp-max", "hash-older", "2026-06-25T09:00:00+00:00"),
            ("item:added", "proxy", LOWKEYCODES_PROJECT_ID, "smith", "task-max-smith", "hash-middle", "2026-06-25T09:05:00+00:00"),
            ("note:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-smith-max", "hash-newer", "2026-06-25T09:10:00+00:00"),
            ("item:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-forward-audit", "hash-forward", "2026-06-25T09:15:00+00:00"),
            ("item:added", "due_poller", LOWKEYCODES_PROJECT_ID, "max", "task-due-max", "hash-due", "2026-06-25T09:20:00+00:00"),
            ("note:added", "proxy", LOWKEYCODES_PROJECT_ID, "max", "task-<unsafe>&\"", "hash-unsafe", "2026-06-25T09:25:00+00:00"),
        ]
        conn.executemany(
            """
            INSERT INTO events (event_name, source, project_id, agent, todoist_task_id, payload_hash, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            events,
        )
        interactions = [
            (1, "semantic", "Filipp", "max", "Max", "task_assigned", "exact", LOWKEYCODES_PROJECT_ID, "task-filipp-max", "hash-older", "recorded", "responsible_uid=59328091", "2026-06-25T09:00:01+00:00"),
            (2, "semantic", "Max", "smith", "Smith", "task_assigned", "exact", LOWKEYCODES_PROJECT_ID, "task-max-smith", "hash-middle", "recorded", "responsible_uid=29584133", "2026-06-25T09:05:01+00:00"),
            (3, "semantic", "Smith", "max", "Max", "comment_mentioned", "exact", LOWKEYCODES_PROJECT_ID, "task-smith-max", "hash-newer", "recorded", "mention=@Max comment_id=comment-001", "2026-06-25T09:10:01+00:00"),
            (4, "forward", "system", "max", "max", "forward", "exact", LOWKEYCODES_PROJECT_ID, "task-forward-audit", "hash-forward", "http_200", "forwarded", "2026-06-25T09:15:01+00:00"),
            (5, "forward", "system", "max", "Max", "due_triggered", "exact", LOWKEYCODES_PROJECT_ID, "task-due-max", "hash-due", "http_200", "forwarded", "2026-06-25T09:20:01+00:00"),
            (6, "semantic", "uid:<777>&\"", "max", "Max", "comment_mentioned", "unknown_uid", LOWKEYCODES_PROJECT_ID, "task-<unsafe>&\"", "hash-unsafe", "recorded", "mention=<Max> comment_id=<comment-002>", "2026-06-25T09:25:01+00:00"),
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


def _seed_many_timeline_rows(db_path: Path, count: int = 12) -> None:
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
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        rows = [
            (
                index + 1,
                "semantic",
                "Filipp" if index % 2 == 0 else "Max",
                "max" if index % 2 == 0 else "smith",
                "Max" if index % 2 == 0 else "Smith",
                "task_assigned",
                "exact",
                LOWKEYCODES_PROJECT_ID,
                f"task-many-{index:02d}",
                f"hash-many-{index:02d}",
                "recorded",
                "many-row visualization fixture",
                f"2026-06-25T09:{index:02d}:01+00:00",
            )
            for index in range(count)
        ]
        conn.executemany(
            """
            INSERT INTO interactions (
                event_row_id, interaction_type, actor, agent, target, interaction_kind, confidence,
                project_id, todoist_task_id, payload_hash, status, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def test_control_page_has_exact_main_sections_and_gate_controls(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    todoist_proxy_fixture.control_config_file.write_text(json.dumps({"agents": {"max": {"enabled": False}}}) + "\n")

    html = _page(control_ui, todoist_proxy_fixture.control_home)
    parser = ControlPageParser()
    parser.feed(html)

    assert parser.main_sections == ["Controls", "Timeline", "Event ledger", "Session insights", "Routing rules"]
    assert "data-scope=\"global\"" in html
    assert "data-scope=\"event\"" in html
    assert "data-form=\"project\"" in html
    assert "data-scope=\"agent\"" in html
    assert 'data-scope="agent" data-name="max" data-enabled="false"' in html
    assert 'id="timeline-expand-toggle"' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-controls="timeline-frame"' in html
    assert "Expand timeline" in html
    assert 'id="timeline" class="timeline-section" data-main-section="Timeline" data-expanded="false"' in html
    assert 'id="timeline-frame" class="timeline-frame"' in html
    assert 'section.classList.toggle("is-expanded", expanded)' in html
    assert 'document.body.classList.toggle("timeline-expanded", expanded)' in html
    assert 'button.textContent = expanded ? "Collapse timeline" : "Expand timeline"' in html
    assert 'data-tree-mode="event"' in html
    assert 'class="tree-canvas"' in html
    assert 'class="tree-graph"' in html
    assert 'function renderTreeLayout(tree, focusInteractionId = "")' in html
    assert 'function buildEventGraph(taskNode, focusInteractionId)' in html
    assert 'data-inspect-event=' in html
    assert 'function inspectTreeNode(eventNodeId)' in html
    assert 'tree-node.is-selected' in html
    assert 'selected.classList.add("is-selected")' in html
    assert 'class="tree-inspector" id="tree-inspector"' in html
    assert 'Event tree: each circle is one assignment or mention event' in html
    assert 'Decision tree: each circle is a delegated task' not in html
    assert 'data-tree-mode="decision"' not in html
    assert 'data-inspect-task=' not in html
    assert 'tree-card' not in html
    assert '.tree-children' not in html


def test_timeline_svg_renders_semantic_edges_without_delivery_fanout(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_ui_ledger(todoist_proxy_fixture.interaction_db_file)

    html = _page(control_ui, todoist_proxy_fixture.control_home)
    parser = ControlPageParser()
    parser.feed(html)

    assert parser.agent_columns == ["Filipp", "Max", "Abra", "Smith", "Hausmeister", "System", "Unknown"]
    arrow_tags = re.findall(r'<path class="timeline-arrow [^>]+data-event-id="\d+"[^>]+>', html)
    arrow_matches = re.findall(r'<path class="timeline-arrow ([^"]+)"[^>]+data-event-id="(\d+)"[^>]+data-y="(\d+)"', html)
    assert len(arrow_matches) == 5
    assert len(arrow_tags) == 5
    assert {state for state, _, _ in arrow_matches} == {"forwarded"}
    assert all(re.search(r'd="M \d+ \d+ L \d+ \d+"', tag) for tag in arrow_tags)
    assert all(" C " not in tag for tag in arrow_tags)
    assert 'data-actor="Filipp" data-target="Max"' in html
    assert 'data-actor="Max" data-target="Smith"' in html
    assert 'data-actor="Smith" data-target="Max"' in html
    assert 'data-actor="system" data-target="Max" data-actor-column="System" data-target-column="Max"' in html
    assert 'data-actor="uid:&lt;777&gt;&amp;&quot;" data-target="Max" data-actor-column="Unknown"' in html
    assert 'data-kind="task_assigned" data-task-id="task-filipp-max"' in html
    assert 'data-kind="comment_mentioned" data-task-id="task-smith-max"' in html
    assert 'data-kind="due_triggered" data-task-id="task-due-max"' in html
    assert 'data-event-id="4"' not in html
    assert "task_assigned" in html
    assert "comment_mentioned" in html
    assert "due_triggered" in html
    assert "2026-06-25 09:00" in html
    assert "2026-06-25 09:25" in html
    assert "newest ↑" in html
    assert "oldest ↓" in html
    assert "Filipp → Max · task_assigned" in html
    assert "Max → Smith · task_assigned" in html
    assert "event #1 · task task-filipp-max" in html
    assert "event #5 · task task-due-max" in html
    assert "task:task-filipp-max" not in html
    assert "task:task-max-smith" not in html
    assert "task:task-due-max" not in html
    assert "#1 Filipp" not in html
    assert "task-filipp-max" in html
    assert "task-due-max" in html
    assert "task-&lt;unsafe&gt;&amp;&quot;" in html
    assert "mention=&lt;Max&gt; comment_id=&lt;comment-002&gt;" in html
    assert "task-<unsafe>" not in html
    assert "mention=<Max>" not in html

    y_by_event = {event_id: int(y) for _, event_id, y in arrow_matches}
    assert y_by_event["3"] < y_by_event["1"]
    assert y_by_event["6"] < y_by_event["5"]
    assert min(y_by_event.values()) >= 58
    assert y_by_event["1"] - y_by_event["2"] >= 70


def test_timeline_svg_grows_vertically_for_many_rows(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_many_timeline_rows(todoist_proxy_fixture.interaction_db_file)

    html = _page(control_ui, todoist_proxy_fixture.control_home)
    viewbox_match = re.search(r'<svg id="timeline-svg" viewBox="0 0 (\d+) (\d+)"', html)
    arrow_matches = re.findall(r'<path class="timeline-arrow ([^"]+)"[^>]+data-event-id="(\d+)"[^>]+data-y="(\d+)"', html)

    assert viewbox_match is not None
    assert int(viewbox_match.group(1)) == 1040
    assert int(viewbox_match.group(2)) > 460
    assert "max-height:460px" in html
    assert len(arrow_matches) == 12

    y_by_event = {event_id: int(y) for _, event_id, y in arrow_matches}
    assert y_by_event["12"] < y_by_event["1"]
    assert min(abs(first - second) for first, second in zip(sorted(y_by_event.values()), sorted(y_by_event.values())[1:])) >= 70
    assert "2026-06-25 09:00" in html
    assert "2026-06-25 09:11" in html
    assert "event #12 · task task-many-11" in html


def test_event_ledger_lists_events_and_routing_outcomes(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    _seed_ui_ledger(todoist_proxy_fixture.interaction_db_file)

    html = _page(control_ui, todoist_proxy_fixture.control_home)

    assert "Recent events" in html
    assert "Routing outcomes" in html
    assert "note:added" in html
    assert "Filipp -> Max" in html or "Filipp -&gt; Max" in html
    assert "Smith -> Max" in html or "Smith -&gt; Max" in html
    assert "comment_id=comment-001" in html
