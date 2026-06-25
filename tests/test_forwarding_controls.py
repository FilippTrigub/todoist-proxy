"""Proxy forwarding controls for per-target suppression and retry semantics."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from conftest import LOWKEYCODES_PROJECT_ID, TodoistProxyFixture
from test_proxy_webhook import RecordingSession, _request


def _module():
    return importlib.reload(importlib.import_module("proxy"))


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def _rows(db_path: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


def test_agent_disabled_suppresses_only_that_target_and_forwards_remaining(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_config(todoist_proxy_fixture.control_config_file, {"agents": {"abra": {"enabled": False}}})
    proxy = _module()
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session)))

    assert response.status == 200
    assert sorted(url.rsplit("/", 1)[-1] for url in session.urls) == ["max-lowkeycodes", "smith-lowkeycodes"]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT agent, status, reason FROM interactions WHERE interaction_type = 'forward' ORDER BY agent",
    ) == [
        ("abra", "suppressed", "agent_disabled:abra"),
        ("max", "http_200", "forwarded"),
        ("smith", "http_200", "forwarded"),
    ]


def test_event_disabled_suppresses_all_targets_and_returns_200_record_only(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    _write_config(todoist_proxy_fixture.control_config_file, {"events": {"note:added": False}})
    proxy = _module()
    async def resolve_project_id(*args: Any, **kwargs: Any) -> str:
        return LOWKEYCODES_PROJECT_ID

    monkeypatch.setattr(proxy, "_resolve_project_id", resolve_project_id)
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["note_added"], session)))

    assert response.status == 200
    assert response.text == "ok"
    assert session.urls == []
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [
        ("abra-lowkeycodes", 0, "event_disabled:note:added"),
        ("max-lowkeycodes", 0, "event_disabled:note:added"),
        ("smith-lowkeycodes", 0, "event_disabled:note:added"),
    ]


def test_disabled_targets_do_not_mask_all_enabled_forwarding_failures(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_config(todoist_proxy_fixture.control_config_file, {"agents": {"abra": {"enabled": False}}})
    proxy = _module()
    session = RecordingSession({"max-lowkeycodes": 503, "smith-lowkeycodes": 504})

    response = asyncio.run(proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session)))

    assert response.status == 502
    assert response.text == "all upstream targets failed"
    assert sorted(url.rsplit("/", 1)[-1] for url in session.urls) == ["max-lowkeycodes", "smith-lowkeycodes"]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT agent, status, reason FROM interactions WHERE interaction_type = 'forward' ORDER BY agent",
    ) == [
        ("abra", "suppressed", "agent_disabled:abra"),
        ("max", "http_503", "forward_failed"),
        ("smith", "http_504", "forward_failed"),
    ]


def test_one_enabled_success_keeps_existing_200_semantics_with_disabled_target(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_config(todoist_proxy_fixture.control_config_file, {"agents": {"abra": {"enabled": False}}})
    proxy = _module()
    session = RecordingSession({"max-lowkeycodes": 503, "smith-lowkeycodes": 200})

    response = asyncio.run(proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session)))

    assert response.status == 200
    assert response.text == "ok"
    assert sorted(url.rsplit("/", 1)[-1] for url in session.urls) == ["max-lowkeycodes", "smith-lowkeycodes"]
