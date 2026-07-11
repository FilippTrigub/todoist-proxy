"""Shared test fixtures for the Todoist -> Hermes control surface."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LIVE_HERMES_HOME = Path("/home/filipp/.hermes")
LOWKEYCODES_PROJECT_ID = "6gmpjVFv2wVG7XJQ"
INBOX_PROJECT_ID = "6ggFh66x4WXVVqGH"
UNKNOWN_PROJECT_ID = "unknown-project"

V1_AGENT_MAP: dict[str, dict[str, str | None]] = {
    "max": {
        "uid": "59328091",
        "role": "CEO",
        "subscription": "max-lowkeycodes",
        "project_id": LOWKEYCODES_PROJECT_ID,
    },
    "abra": {
        "uid": "15795569",
        "role": "CMO",
        "subscription": "abra-lowkeycodes",
        "project_id": LOWKEYCODES_PROJECT_ID,
    },
    "smith": {
        "uid": "29584133",
        "role": "DevOps",
        "subscription": "smith-lowkeycodes",
        "project_id": LOWKEYCODES_PROJECT_ID,
    },
    "hausmeister": {
        "uid": "59138424",
        "role": None,
        "subscription": "hausmeister-inbox",
        "project_id": INBOX_PROJECT_ID,
    },
    "system": {
        "uid": None,
        "role": "system",
        "subscription": "system",
        "project_id": None,
    },
    "unknown": {
        "uid": None,
        "role": None,
        "subscription": "unknown",
        "project_id": UNKNOWN_PROJECT_ID,
    },
}

V1_MENTION_ALIASES: dict[str, tuple[str, ...]] = {
    "max": ("@Max", "Max", "Max | CEO"),
    "abra": ("@Abra", "Abra", "Abra | CMO"),
    "smith": ("@Smith", "Smith", "Smith | DevOps"),
}

TIME_AXIS_EVENTS: tuple[dict[str, int | str], ...] = (
    {"id": "older-event", "label": "older", "axis": 10},
    {"id": "newer-event", "label": "newer/current", "axis": 20},
)


@dataclass(frozen=True)
class TodoistProxyFixture:
    hermes_home: Path
    control_home: Path
    routing_file: Path
    disable_file: Path
    due_poller_db: Path
    report_cadence_db: Path
    unblock_file: Path
    control_config_file: Path
    interaction_db_file: Path
    subscription_files: tuple[Path, ...]
    payloads: dict[str, dict[str, Any]]


def _subscription_payload(name: str, events: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "events": events,
        "deliver": "telegram",
        "secret": "INSECURE_NO_AUTH",
        "description": f"Test fixture subscription for {name}",
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def todoist_proxy_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TodoistProxyFixture:
    hermes_home = tmp_path / "hermes"
    control_home = tmp_path / "control"
    routing_file = hermes_home / "todoist-routing.json"
    disable_file = hermes_home / "todoist-proxy.disabled"
    due_poller_db = hermes_home / "state" / "todoist_due_poller.db"
    report_cadence_db = hermes_home / "state" / "report_cadence.db"
    unblock_file = hermes_home / "todoist-due-poller-unblock.json"
    control_config_file = control_home / "todoist-control.json"
    interaction_db_file = control_home / "todoist_interactions.db"

    routing = {
        "routes": {
            INBOX_PROJECT_ID: ["hausmeister-inbox"],
            LOWKEYCODES_PROJECT_ID: [
                "max-lowkeycodes",
                "abra-lowkeycodes",
                "smith-lowkeycodes",
            ],
        },
        "upstreams": {
            "hausmeister-inbox": "http://127.0.0.1:8644",
            "max-lowkeycodes": "http://127.0.0.1:8644",
            "abra-lowkeycodes": "http://127.0.0.1:8644",
            "smith-lowkeycodes": "http://127.0.0.1:8644",
        },
    }
    _write_json(routing_file, routing)
    _write_json(
        unblock_file,
        {
            "hausmeister-inbox": {
                "state_file": str(hermes_home / "state" / "todoist_shared_inbox_media_note_hook.json"),
                "id_fields": ["handled_task_ids", "seen_task_ids"],
            }
        },
    )

    root_subscriptions = hermes_home / "webhook_subscriptions.json"
    _write_json(
        root_subscriptions,
        {
            "subscriptions": {
                "hausmeister-inbox": _subscription_payload("hausmeister-inbox", ["item:added"]),
            }
        },
    )

    profile_subscription_files: list[Path] = []
    for profile in ("max", "abra", "smith"):
        subscription = str(V1_AGENT_MAP[profile]["subscription"])
        path = hermes_home / "profiles" / profile / "webhook_subscriptions.json"
        _write_json(
            path,
            {
                "subscriptions": {
                    subscription: _subscription_payload(
                        subscription,
                        ["item:added", "item:updated", "item:completed", "item:uncompleted", "note:added"],
                    )
                }
            },
        )
        profile_subscription_files.append(path)

    _write_json(
        control_config_file,
        {
            "control_home": str(control_home),
            "routing_file": str(routing_file),
            "agents": sorted(V1_AGENT_MAP),
        },
    )
    interaction_db_file.parent.mkdir(parents=True, exist_ok=True)
    interaction_db_file.touch()

    payloads: dict[str, dict[str, Any]] = {
        "item_added": {
            "event_name": "item:added",
            "event_data": {
                "id": "task-normal-001",
                "content": "Prepare LowKeyCodes weekly review",
                "project_id": LOWKEYCODES_PROJECT_ID,
                "responsible_uid": V1_AGENT_MAP["max"]["uid"],
                "creator_uid": "15611160",
                "priority": 1,
                "labels": [],
            },
        },
        "note_added": {
            "event_name": "note:added",
            "event_data": {
                "id": "note-001",
                "content": "@Smith please check the deployment logs",
                "item_id": "task-normal-001",
                "posted_uid": "15611160",
            },
        },
        "future_due_item_added": {
            "event_name": "item:added",
            "event_data": {
                "id": "task-future-001",
                "content": "Follow up next month",
                "project_id": LOWKEYCODES_PROJECT_ID,
                "responsible_uid": V1_AGENT_MAP["abra"]["uid"],
                "creator_uid": "15611160",
                "priority": 2,
                "labels": ["scheduled"],
                "due": {"date": "2099-01-01", "string": "Jan 1 2099"},
            },
        },
        "due_poll_item_added": {
            "event_name": "item:added",
            "event_data": {
                "id": "task-due-poll-001",
                "content": "Synthetic due poll delivery",
                "description": "",
                "project_id": INBOX_PROJECT_ID,
                "section_id": None,
                "parent_id": None,
                "responsible_uid": None,
                "creator_uid": "15611160",
                "priority": 1,
                "labels": [],
                "due": {"date": "2026-06-25", "string": "today"},
                "_synthetic": True,
                "_trigger": "due_poll",
            },
        },
    }

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CONTROL_HOME", str(control_home))
    monkeypatch.setenv("TODOIST_ROUTING_FILE", str(routing_file))
    monkeypatch.setenv("TODOIST_DISABLE_FILE", str(disable_file))
    monkeypatch.setenv("TODOIST_DUE_POLLER_DB", str(due_poller_db))
    monkeypatch.setenv("REPORT_CADENCE_DB", str(report_cadence_db))
    monkeypatch.setenv("TODOIST_DUE_POLLER_UNBLOCK_FILE", str(unblock_file))
    monkeypatch.setenv("TODOIST_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("TODOIST_API_KEY", "test-api-key")

    return TodoistProxyFixture(
        hermes_home=hermes_home,
        control_home=control_home,
        routing_file=routing_file,
        disable_file=disable_file,
        due_poller_db=due_poller_db,
        report_cadence_db=report_cadence_db,
        unblock_file=unblock_file,
        control_config_file=control_config_file,
        interaction_db_file=interaction_db_file,
        subscription_files=(root_subscriptions, *profile_subscription_files),
        payloads=payloads,
    )
