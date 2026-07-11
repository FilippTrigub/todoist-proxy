"""Security invariants for the local control UI API."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from conftest import LIVE_HERMES_HOME, TodoistProxyFixture


def _module():
    return importlib.reload(importlib.import_module("control_ui"))


def test_post_toggle_succeeds_without_any_auth_header(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    """The control UI binds 127.0.0.1 only, so it deliberately has no auth
    gate on its own write endpoints — a client-side prompt for a pasted
    token is friction with no real protection benefit for a local-only
    tool. This guards against silently reintroducing one."""

    control_ui = _module()
    body = json.dumps(
        {"scope": "global", "key": "forwarding_enabled", "enabled": False}
    ).encode("utf-8")

    response = control_ui.handle_api_request(
        "POST",
        "/api/config/toggle",
        body=body,
        headers={},
        control_home=todoist_proxy_fixture.control_home,
    )

    assert response.status == 200
    assert json.loads(todoist_proxy_fixture.control_config_file.read_text())["global"]["forwarding_enabled"] is False


def test_post_report_cadence_config_succeeds_without_any_auth_header(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    body = json.dumps({"mrr_target_eur": 500}).encode("utf-8")

    response = control_ui.handle_api_request(
        "POST",
        "/api/report-cadence/config",
        body=body,
        headers={},
        control_home=todoist_proxy_fixture.control_home,
    )

    assert response.status == 200
    assert json.loads(todoist_proxy_fixture.control_config_file.read_text())["report_cadence"] == {
        "mrr_target_eur": 500.0
    }


def test_state_changing_get_is_rejected_and_noop(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    before = todoist_proxy_fixture.control_config_file.read_text()

    response = control_ui.handle_api_request(
        "GET",
        "/api/config/toggle?scope=global&key=forwarding_enabled&enabled=false",
        control_home=todoist_proxy_fixture.control_home,
    )

    assert response.status == 405
    assert todoist_proxy_fixture.control_config_file.read_text() == before


def test_cors_headers_are_not_permissive_on_api_responses(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()

    response = control_ui.handle_api_request(
        "GET",
        "/api/status",
        headers={"Origin": "https://example.test"},
        control_home=todoist_proxy_fixture.control_home,
    )

    assert response.status == 200
    assert not any(key.lower() == "access-control-allow-origin" for key, _ in response.headers)


def test_api_does_not_create_runtime_artifacts_under_live_hermes(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    body = json.dumps(
        {"scope": "project_agent", "project_id": "project-1", "agent": "max", "enabled": False}
    ).encode("utf-8")

    response = control_ui.handle_api_request(
        "POST",
        "/api/config/toggle",
        body=body,
        control_home=todoist_proxy_fixture.control_home,
    )
    created_paths = [path for path in todoist_proxy_fixture.control_home.rglob("*") if path.is_file()]

    assert response.status == 200
    assert created_paths
    assert all(Path(path).resolve().is_relative_to(todoist_proxy_fixture.control_home.resolve()) for path in created_paths)
    assert all(not str(path).startswith(str(LIVE_HERMES_HOME)) for path in created_paths)
