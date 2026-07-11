"""Control config loading and scoped forwarding gate tests."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from conftest import LOWKEYCODES_PROJECT_ID, TodoistProxyFixture


def _module():
    return importlib.reload(importlib.import_module("control_ledger"))


def _write_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def test_control_home_defaults_to_dedicated_control_directory(monkeypatch) -> None:
    monkeypatch.delenv("CONTROL_HOME", raising=False)
    control_ledger = _module()

    assert control_ledger.resolve_control_home() == Path("/home/filipp/todoist-hermes-control")
    assert control_ledger.control_config_path() == Path(
        "/home/filipp/todoist-hermes-control/todoist-control.json"
    )
    assert control_ledger.ledger_db_path() == Path(
        "/home/filipp/todoist-hermes-control/todoist_interactions.db"
    )


def test_control_home_env_override_uses_temp_fixture(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ledger = _module()

    assert control_ledger.resolve_control_home() == todoist_proxy_fixture.control_home
    assert control_ledger.control_config_path() == todoist_proxy_fixture.control_config_file
    assert control_ledger.ledger_db_path() == todoist_proxy_fixture.interaction_db_file


def test_missing_config_defaults_to_forwarding_enabled(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.control_config_file.unlink()
    control_ledger = _module()

    decision = control_ledger.evaluate_forwarding(
        event_name="item:added",
        project_id=LOWKEYCODES_PROJECT_ID,
        agent="max",
        sentinel_path=todoist_proxy_fixture.disable_file,
    )

    assert decision.enabled is True
    assert decision.reason == "missing_config_forwarding_enabled"
    assert decision.config_status == "missing"


def test_invalid_config_does_not_crash_and_preserves_forwarding_compatibility(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.control_config_file.write_text("{not-json")
    control_ledger = _module()

    decision = control_ledger.evaluate_forwarding(
        event_name="item:added",
        project_id=LOWKEYCODES_PROJECT_ID,
        agent="max",
        sentinel_path=todoist_proxy_fixture.disable_file,
    )

    assert decision.enabled is True
    assert decision.reason == "invalid_config_forwarding_enabled"
    assert decision.config_status == "invalid"


def test_legacy_disable_sentinel_overrides_json_config(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_config(
        todoist_proxy_fixture.control_config_file,
        {"global": {"forwarding_enabled": True}},
    )
    todoist_proxy_fixture.disable_file.touch()
    control_ledger = _module()

    decision = control_ledger.evaluate_forwarding(
        event_name="item:added",
        project_id=LOWKEYCODES_PROJECT_ID,
        agent="max",
        sentinel_path=todoist_proxy_fixture.disable_file,
    )

    assert decision.enabled is False
    assert decision.reason == "legacy_disable_sentinel_present"


def test_scoped_gates_disable_forwarding_at_each_supported_level(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_config(
        todoist_proxy_fixture.control_config_file,
        {
            "global": {
                "forwarding_enabled": True,
                "due_poller_forwarding_enabled": False,
                "spark_enabled": False,
            },
            "events": {"item:completed": False},
            "projects": {
                "blocked-project": {"enabled": False},
                LOWKEYCODES_PROJECT_ID: {"enabled": True, "agents": {"max": False}},
            },
            "agents": {
                "abra": {"enabled": False},
                "smith": {"enabled": True, "events": {"note:added": False}},
            },
        },
    )
    control_ledger = _module()

    cases = [
        (
            {"event_name": "item:added", "project_id": LOWKEYCODES_PROJECT_ID, "agent": "smith", "source": "due_poller"},
            "global_due_poller_forwarding_disabled",
        ),
        (
            {"event_name": "item:added", "project_id": LOWKEYCODES_PROJECT_ID, "agent": "max", "source": "report_cadence"},
            "global_spark_disabled",
        ),
        (
            {"event_name": "item:completed", "project_id": LOWKEYCODES_PROJECT_ID, "agent": "smith"},
            "event_disabled:item:completed",
        ),
        (
            {"event_name": "item:added", "project_id": "blocked-project", "agent": "smith"},
            "project_disabled:blocked-project",
        ),
        (
            {"event_name": "item:added", "project_id": LOWKEYCODES_PROJECT_ID, "agent": "max"},
            f"project_agent_disabled:{LOWKEYCODES_PROJECT_ID}:max",
        ),
        (
            {"event_name": "item:added", "project_id": LOWKEYCODES_PROJECT_ID, "agent": "abra"},
            "agent_disabled:abra",
        ),
        (
            {"event_name": "note:added", "project_id": LOWKEYCODES_PROJECT_ID, "agent": "smith"},
            "agent_event_disabled:smith:note:added",
        ),
    ]

    for kwargs, reason in cases:
        decision = control_ledger.evaluate_forwarding(
            sentinel_path=todoist_proxy_fixture.disable_file,
            **kwargs,
        )
        assert decision.enabled is False
        assert decision.reason == reason


def test_scoped_gates_default_to_enabled_when_no_gate_blocks(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_config(
        todoist_proxy_fixture.control_config_file,
        {
            "global": {"forwarding_enabled": True},
            "events": {"item:added": True},
            "projects": {LOWKEYCODES_PROJECT_ID: {"enabled": True}},
            "agents": {"smith": {"enabled": True, "events": {"item:added": True}}},
        },
    )
    control_ledger = _module()

    decision = control_ledger.evaluate_forwarding(
        event_name="item:added",
        project_id=LOWKEYCODES_PROJECT_ID,
        agent="smith",
        sentinel_path=todoist_proxy_fixture.disable_file,
    )

    assert decision.enabled is True
    assert decision.reason == "forwarding_enabled"
    assert decision.config_status == "loaded"
