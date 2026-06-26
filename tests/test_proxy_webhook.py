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
from pathlib import Path
from typing import Any

from conftest import LOWKEYCODES_PROJECT_ID, UNKNOWN_PROJECT_ID, TodoistProxyFixture


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
    def __init__(
        self,
        statuses: dict[str, int] | None = None,
        task_project_ids: dict[str, str] | None = None,
    ) -> None:
        self.statuses = statuses or {}
        self.task_project_ids = task_project_ids or {}
        self.urls: list[str] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.urls.append(url)
        status = self.statuses.get(url.rsplit("/", 1)[-1], 200)
        return ResponseContext(status)

    def get(self, url: str, **kwargs: Any) -> Any:
        item_id = url.rsplit("/", 1)[-1]
        project_id = self.task_project_ids.get(item_id, "")
        return JsonResponseContext(200 if project_id else 404, {"project_id": project_id})


class ResponseContext:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> "ResponseContext":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class JsonResponseContext(ResponseContext):
    def __init__(self, status: int, data: dict[str, Any]) -> None:
        super().__init__(status)
        self._data = data

    async def json(self) -> dict[str, Any]:
        return self._data


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


def _semantic_rows(db_path: Path) -> list[tuple[Any, ...]]:
    try:
        return _ledger_rows(
            db_path,
            """
            SELECT actor, target, interaction_kind, confidence, todoist_task_id, reason
            FROM interactions
            WHERE interaction_kind IN ('task_assigned', 'comment_mentioned', 'due_triggered')
            ORDER BY id
            """,
        )
    except sqlite3.OperationalError:
        return []


def _semantic_row_prefixes(db_path: Path) -> list[tuple[Any, ...]]:
    return [row[:5] for row in _semantic_rows(db_path)]


def _payload_copy(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload))


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
    assert _semantic_rows(todoist_proxy_fixture.interaction_db_file) == []
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
        """
        SELECT interaction_type, agent, status, reason
        FROM interactions
        WHERE interaction_type = 'forward'
        ORDER BY agent
        """,
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


def test_item_added_assignment_records_filipp_to_max_semantic_row(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["item_added"])
    payload["event_data"].update(
        {
            "id": "task-filipp-to-max-001",
            "creator_uid": "15611160",
            "responsible_uid": "59328091",
        }
    )

    response = asyncio.run(proxy.handle(_request(proxy, payload)))

    assert response.status == 200
    assert (
        "Filipp",
        "Max",
        "task_assigned",
        "exact",
        payload["event_data"]["id"],
    ) in _semantic_row_prefixes(todoist_proxy_fixture.interaction_db_file)


def test_item_added_assignment_records_max_to_smith_semantic_row(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["item_added"])
    payload["event_data"].update(
        {
            "id": "task-max-to-smith-001",
            "creator_uid": "59328091",
            "responsible_uid": "29584133",
        }
    )

    response = asyncio.run(proxy.handle(_request(proxy, payload)))

    assert response.status == 200
    assert (
        "Max",
        "Smith",
        "task_assigned",
        "exact",
        payload["event_data"]["id"],
    ) in _semantic_row_prefixes(todoist_proxy_fixture.interaction_db_file)


def test_note_added_at_max_records_smith_to_max_with_parent_task_and_comment_id(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-smith-max-001",
            "content": "@Max please review the rollback plan",
            "item_id": "task-parent-001",
            "posted_uid": "29584133",
        }
    )
    session = RecordingSession(task_project_ids={payload["event_data"]["item_id"]: LOWKEYCODES_PROJECT_ID})

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    rows = _semantic_rows(todoist_proxy_fixture.interaction_db_file)
    assert (
        "Smith",
        "Max",
        "comment_mentioned",
        "exact",
        payload["event_data"]["item_id"],
        f"mention=@Max comment_id={payload['event_data']['id']}",
    ) in rows


def test_note_added_multiple_mentions_records_one_row_per_target(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-smith-team-001",
            "content": "@Max @Abra can you both check this?",
            "item_id": "task-parent-002",
            "posted_uid": "29584133",
        }
    )
    session = RecordingSession(task_project_ids={payload["event_data"]["item_id"]: LOWKEYCODES_PROJECT_ID})

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert _semantic_rows(todoist_proxy_fixture.interaction_db_file) == [
        (
            "Smith",
            "Max",
            "comment_mentioned",
            "exact",
            payload["event_data"]["item_id"],
            f"mention=@Max comment_id={payload['event_data']['id']}",
        ),
        (
            "Smith",
            "Abra",
            "comment_mentioned",
            "exact",
            payload["event_data"]["item_id"],
            f"mention=@Abra comment_id={payload['event_data']['id']}",
        ),
    ]


def test_note_added_unknown_poster_falls_back_to_uid_label_and_unknown_confidence(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-unknown-max-001",
            "content": "Max should see this handoff",
            "item_id": "task-parent-003",
            "posted_uid": "99999999",
        }
    )
    session = RecordingSession(task_project_ids={payload["event_data"]["item_id"]: LOWKEYCODES_PROJECT_ID})

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert (
        "uid:99999999",
        "Max",
        "comment_mentioned",
        "unknown_uid",
        payload["event_data"]["item_id"],
        f"mention=Max comment_id={payload['event_data']['id']}",
    ) in _semantic_rows(todoist_proxy_fixture.interaction_db_file)


def test_note_added_without_explicit_mentions_records_no_semantic_rows_but_keeps_audit(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-no-mention-001",
            "content": "maximum smithing effort, but no actual user mention",
            "item_id": "task-parent-004",
            "posted_uid": "29584133",
        }
    )
    session = RecordingSession(task_project_ids={payload["event_data"]["item_id"]: LOWKEYCODES_PROJECT_ID})

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert _semantic_rows(todoist_proxy_fixture.interaction_db_file) == []
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [
        ("abra-lowkeycodes", 1, "forwarding_enabled"),
        ("max-lowkeycodes", 1, "forwarding_enabled"),
        ("smith-lowkeycodes", 1, "forwarding_enabled"),
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
    assert (
        "Filipp",
        "Abra",
        "task_assigned",
        "exact",
        payload["event_data"]["id"],
        f"responsible_uid={payload['event_data']['responsible_uid']}",
    ) in _semantic_rows(todoist_proxy_fixture.interaction_db_file)
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        """
        SELECT interaction_type, agent, project_id, status, reason
        FROM interactions
        WHERE interaction_type = 'routing'
        """,
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
