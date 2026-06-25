"""Document and verify the v1 Todoist/Hermes fixture contract."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from conftest import (
    INBOX_PROJECT_ID,
    LIVE_HERMES_HOME,
    LOWKEYCODES_PROJECT_ID,
    TIME_AXIS_EVENTS,
    UNKNOWN_PROJECT_ID,
    V1_AGENT_MAP,
    V1_MENTION_ALIASES,
    TodoistProxyFixture,
)


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path.resolve().is_relative_to(parent.resolve())


def test_v1_agent_map_and_mention_aliases_are_canonical() -> None:
    assert set(V1_AGENT_MAP) == {"max", "abra", "smith", "hausmeister", "system", "unknown"}
    assert V1_AGENT_MAP["max"]["subscription"] == "max-lowkeycodes"
    assert V1_AGENT_MAP["abra"]["subscription"] == "abra-lowkeycodes"
    assert V1_AGENT_MAP["smith"]["subscription"] == "smith-lowkeycodes"
    assert V1_AGENT_MAP["hausmeister"]["subscription"] == "hausmeister-inbox"
    assert V1_AGENT_MAP["unknown"]["project_id"] == UNKNOWN_PROJECT_ID

    assert V1_MENTION_ALIASES == {
        "max": ("@Max", "Max", "Max | CEO"),
        "abra": ("@Abra", "Abra", "Abra | CMO"),
        "smith": ("@Smith", "Smith", "Smith | DevOps"),
    }


def test_temp_homes_map_to_current_import_time_globals(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = importlib.reload(importlib.import_module("proxy"))
    due_poller = importlib.reload(importlib.import_module("due_poller"))

    assert proxy.ROUTING_FILE == todoist_proxy_fixture.routing_file
    assert proxy.DISABLE_FILE == todoist_proxy_fixture.disable_file
    assert due_poller.ROUTING_FILE == todoist_proxy_fixture.routing_file
    assert due_poller.DB_FILE == todoist_proxy_fixture.due_poller_db
    assert due_poller.UNBLOCK_FILE == todoist_proxy_fixture.unblock_file


def test_fixture_files_are_isolated_and_match_current_routing_shape(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    routing = json.loads(todoist_proxy_fixture.routing_file.read_text())
    assert routing["routes"] == {
        INBOX_PROJECT_ID: ["hausmeister-inbox"],
        LOWKEYCODES_PROJECT_ID: ["max-lowkeycodes", "abra-lowkeycodes", "smith-lowkeycodes"],
    }

    all_fixture_paths = (
        todoist_proxy_fixture.hermes_home,
        todoist_proxy_fixture.control_home,
        todoist_proxy_fixture.routing_file,
        todoist_proxy_fixture.disable_file,
        todoist_proxy_fixture.due_poller_db,
        todoist_proxy_fixture.unblock_file,
        todoist_proxy_fixture.control_config_file,
        todoist_proxy_fixture.interaction_db_file,
        *todoist_proxy_fixture.subscription_files,
    )
    for path in all_fixture_paths:
        assert not str(path).startswith(str(LIVE_HERMES_HOME))

    assert _is_relative_to(todoist_proxy_fixture.routing_file, todoist_proxy_fixture.hermes_home)
    assert _is_relative_to(todoist_proxy_fixture.unblock_file, todoist_proxy_fixture.hermes_home)
    assert _is_relative_to(todoist_proxy_fixture.due_poller_db, todoist_proxy_fixture.hermes_home)
    for subscription_file in todoist_proxy_fixture.subscription_files:
        assert subscription_file.exists()
        assert _is_relative_to(subscription_file, todoist_proxy_fixture.hermes_home)


def test_control_home_paths_are_not_ui_state_under_hermes(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    assert todoist_proxy_fixture.control_config_file.exists()
    assert todoist_proxy_fixture.interaction_db_file.exists()
    assert todoist_proxy_fixture.control_config_file.name == "todoist-control.json"
    assert todoist_proxy_fixture.interaction_db_file.name == "todoist_interactions.db"
    assert _is_relative_to(todoist_proxy_fixture.control_config_file, todoist_proxy_fixture.control_home)
    assert _is_relative_to(todoist_proxy_fixture.interaction_db_file, todoist_proxy_fixture.control_home)
    assert not _is_relative_to(todoist_proxy_fixture.control_config_file, todoist_proxy_fixture.hermes_home)
    assert not _is_relative_to(todoist_proxy_fixture.interaction_db_file, todoist_proxy_fixture.hermes_home)
    assert not str(todoist_proxy_fixture.control_config_file).startswith(str(LIVE_HERMES_HOME))
    assert not str(todoist_proxy_fixture.interaction_db_file).startswith(str(LIVE_HERMES_HOME))


def test_payload_fixtures_cover_v1_webhook_and_due_poller_events(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    payloads = todoist_proxy_fixture.payloads
    assert payloads["item_added"]["event_name"] == "item:added"
    assert payloads["note_added"]["event_name"] == "note:added"
    assert "item_id" in payloads["note_added"]["event_data"]
    assert "project_id" not in payloads["note_added"]["event_data"]

    future_due = payloads["future_due_item_added"]
    assert future_due["event_name"] == "item:added"
    assert future_due["event_data"]["due"]["date"] == "2099-01-01"

    synthetic = payloads["due_poll_item_added"]
    assert synthetic["event_name"] == "item:added"
    assert synthetic["event_data"]["_synthetic"] is True
    assert synthetic["event_data"]["_trigger"] == "due_poll"


def test_due_poller_build_event_matches_synthetic_fixture_shape(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    due_poller = importlib.reload(importlib.import_module("due_poller"))
    task = {
        "id": "task-due-poll-001",
        "content": "Synthetic due poll delivery",
        "project_id": INBOX_PROJECT_ID,
        "added_by_uid": "15611160",
        "priority": 1,
        "labels": [],
        "due": {"date": "2026-06-25", "string": "today"},
    }

    event = due_poller._build_event(task)
    assert event["event_name"] == todoist_proxy_fixture.payloads["due_poll_item_added"]["event_name"]
    assert event["event_data"]["id"] == task["id"]
    assert event["event_data"]["_synthetic"] is True
    assert event["event_data"]["_trigger"] == "due_poll"


def test_time_axis_convention_newer_or_current_events_sort_higher() -> None:
    older, newer = TIME_AXIS_EVENTS
    assert older["label"] == "older"
    assert newer["label"] == "newer/current"
    assert older["axis"] < newer["axis"]
