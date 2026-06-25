"""Proxy webhook invariants around HMAC, routing outcomes, and ledger writes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from conftest import UNKNOWN_PROJECT_ID, TodoistProxyFixture


@dataclass
class StubRequest:
    body: bytes
    signature: str
    app: dict[str, Any]
    method: str = "POST"
    path: str = "/webhooks/todoist"

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Todoist-Hmac-SHA256": self.signature, "Host": "example.test"}

    async def read(self) -> bytes:
        return self.body


class RecordingSession:
    def __init__(self, statuses: dict[str, int] | None = None) -> None:
        self.statuses = statuses or {}
        self.urls: list[str] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.urls.append(url)
        status = self.statuses.get(url.rsplit("/", 1)[-1], 200)
        return ResponseContext(status)


class ResponseContext:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> "ResponseContext":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _module():
    return importlib.reload(importlib.import_module("proxy"))


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _signature(secret: bytes, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest()).decode()


def _request(proxy, payload: dict[str, Any], session: RecordingSession | None = None) -> StubRequest:
    body = _body(payload)
    secret = b"test-secret"
    return StubRequest(
        body=body,
        signature=_signature(secret, body),
        app={"secret": secret, "session": session or RecordingSession(), "todoist_api_key": "test-api-key"},
    )


def _ledger_rows(db_path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


def test_invalid_signature_never_forwards_or_records_payload(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    session = RecordingSession()
    payload = todoist_proxy_fixture.payloads["item_added"]
    body = _body(payload)
    request = StubRequest(
        body=body,
        signature="not-valid",
        app={"secret": b"test-secret", "session": session, "todoist_api_key": "test-api-key"},
    )

    response = asyncio.run(proxy.handle(request))

    assert response.status == 401
    assert response.text == "invalid signature"
    assert session.urls == []
    assert _ledger_rows(todoist_proxy_fixture.interaction_db_file, "SELECT name FROM sqlite_master") == []


def test_valid_signed_payload_records_event_before_forwarding(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = todoist_proxy_fixture.payloads["item_added"]
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert response.text == "ok"
    assert len(session.urls) == 3
    event_rows = _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT event_name, project_id, todoist_task_id FROM events",
    )
    routing_rows = _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    )
    interaction_rows = _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT interaction_type, agent, status, reason FROM interactions ORDER BY agent",
    )

    assert event_rows == [("item:added", payload["event_data"]["project_id"], payload["event_data"]["id"])]
    assert routing_rows == [
        ("abra-lowkeycodes", 1, "forwarding_enabled"),
        ("max-lowkeycodes", 1, "forwarding_enabled"),
        ("smith-lowkeycodes", 1, "forwarding_enabled"),
    ]
    assert interaction_rows == [
        ("forward", "abra", "http_200", "forwarded"),
        ("forward", "max", "http_200", "forwarded"),
        ("forward", "smith", "http_200", "forwarded"),
    ]


def test_future_due_item_added_records_deferred_outcome_and_does_not_forward(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = todoist_proxy_fixture.payloads["future_due_item_added"]
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert response.text == "deferred: due in future"
    assert session.urls == []
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT interaction_type, agent, project_id, status, reason FROM interactions",
    ) == [("routing", "", payload["event_data"]["project_id"], "deferred", "due_in_future")]


def test_no_route_records_unrouted_outcome_and_returns_200(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = {
        "event_name": "item:added",
        "event_data": {"id": "task-unrouted-001", "project_id": UNKNOWN_PROJECT_ID},
    }
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert response.text == "no route"
    assert session.urls == []
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT interaction_type, project_id, status, reason FROM interactions",
    ) == [("routing", UNKNOWN_PROJECT_ID, "unrouted", "no_route")]
