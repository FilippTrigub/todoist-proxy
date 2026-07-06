"""Security invariants for the local control UI API."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from conftest import LIVE_HERMES_HOME, TodoistProxyFixture


def _module():
    return importlib.reload(importlib.import_module("control_ui"))


def _json(response):
    return json.loads(response.body.decode("utf-8"))


def test_post_toggle_requires_custom_token_header(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    body = json.dumps(
        {"scope": "global", "key": "forwarding_enabled", "enabled": False}
    ).encode("utf-8")

    missing = control_ui.handle_api_request(
        "POST",
        "/api/config/toggle",
        body=body,
        headers={},
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    wrong = control_ui.handle_api_request(
        "POST",
        "/api/config/toggle",
        body=body,
        headers={control_ui.TOKEN_HEADER: "wrong"},
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )

    assert missing.status == 403
    assert wrong.status == 403
    assert json.loads(todoist_proxy_fixture.control_config_file.read_text()).get("global") is None


def test_post_toggle_accepts_token_header_case_insensitively(
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
        headers={control_ui.TOKEN_HEADER.lower(): "test-token"},
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )

    assert response.status == 200
    assert _json(response)["changed"] == "global.forwarding_enabled"
    assert json.loads(todoist_proxy_fixture.control_config_file.read_text())["global"]["forwarding_enabled"] is False


def test_state_changing_get_is_rejected_and_noop(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()
    before = todoist_proxy_fixture.control_config_file.read_text()

    response = control_ui.handle_api_request(
        "GET",
        "/api/config/toggle?scope=global&key=forwarding_enabled&enabled=false",
        headers={control_ui.TOKEN_HEADER: "test-token"},
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
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
        token="test-token",
    )

    assert response.status == 200
    assert not any(key.lower() == "access-control-allow-origin" for key, _ in response.headers)


def test_token_resolution_uses_env_without_writing_token_file(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TODOIST_CONTROL_UI_TOKEN", "env-token")
    control_ui = _module()

    token = control_ui.resolve_token(todoist_proxy_fixture.control_home)

    assert token == "env-token"
    assert not (todoist_proxy_fixture.control_home / control_ui.DEFAULT_TOKEN_FILE_NAME).exists()


def test_generated_token_file_stays_under_control_home(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    monkeypatch.delenv("TODOIST_CONTROL_UI_TOKEN", raising=False)
    monkeypatch.delenv("TODOIST_CONTROL_UI_TOKEN_FILE", raising=False)
    control_ui = _module()

    token = control_ui.resolve_token(todoist_proxy_fixture.control_home)
    token_file = todoist_proxy_fixture.control_home / control_ui.DEFAULT_TOKEN_FILE_NAME

    assert token
    assert token_file.exists()
    assert token_file.read_text().strip() == token
    assert token_file.resolve().is_relative_to(todoist_proxy_fixture.control_home.resolve())
    assert not str(token_file).startswith(str(LIVE_HERMES_HOME))


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
        headers={control_ui.TOKEN_HEADER: "test-token"},
        control_home=todoist_proxy_fixture.control_home,
        token="test-token",
    )
    created_paths = [path for path in todoist_proxy_fixture.control_home.rglob("*") if path.is_file()]

    assert response.status == 200
    assert created_paths
    assert all(Path(path).resolve().is_relative_to(todoist_proxy_fixture.control_home.resolve()) for path in created_paths)
    assert all(not str(path).startswith(str(LIVE_HERMES_HOME)) for path in created_paths)


def test_status_and_config_do_not_expose_token_values(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    control_ui = _module()

    status = control_ui.handle_api_request(
        "GET",
        "/api/status",
        control_home=todoist_proxy_fixture.control_home,
        token="super-secret-token",
    )
    effective = control_ui.handle_api_request(
        "GET",
        "/api/config/effective",
        control_home=todoist_proxy_fixture.control_home,
        token="super-secret-token",
    )

    assert "super-secret-token" not in status.body.decode("utf-8")
    assert "super-secret-token" not in effective.body.decode("utf-8")
