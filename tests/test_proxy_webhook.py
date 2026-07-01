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
from urllib.parse import parse_qs, urlparse

from conftest import LOWKEYCODES_PROJECT_ID, UNKNOWN_PROJECT_ID, TodoistProxyFixture


SECTION_MAX = "6gpFcCwF29V6QXxx"
SECTION_ABRA = "6gpFcCvfqGxWcqwx"
SECTION_SMITH = "6gpFcCxmc39r8MrQ"


@dataclass
class StubRequest:
    body: bytes
    signature: str
    app: dict[str, Any]
    extra_headers: dict[str, str] | None = None
    method: str = "POST"
    path: str = "/webhooks/todoist"

    @property
    def headers(self) -> dict[str, str]:
        headers = {"X-Todoist-Hmac-SHA256": self.signature, "Host": "example.test"}
        if self.extra_headers:
            headers.update(self.extra_headers)
        return headers

    async def read(self) -> bytes:
        return self.body


class RecordingSession:
    def __init__(
        self,
        statuses: dict[str, int] | None = None,
        task_project_ids: dict[str, str] | None = None,
        task_contexts: dict[str, dict[str, Any]] | None = None,
        task_lists_by_parent: dict[str, list[dict[str, Any]]] | None = None,
        task_statuses: dict[str, int] | None = None,
    ) -> None:
        self.statuses = statuses or {}
        self.task_project_ids = task_project_ids or {}
        self.task_contexts = task_contexts or {}
        self.task_lists_by_parent = task_lists_by_parent or {}
        self.task_statuses = task_statuses or {}
        self.urls: list[str] = []
        self.get_urls: list[str] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.urls.append(url)
        status = self.statuses.get(url.rsplit("/", 1)[-1], 200)
        return ResponseContext(status)

    def get(self, url: str, **kwargs: Any) -> Any:
        self.get_urls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if "parent_id" in query:
            parent_id = query["parent_id"][0]
            return JsonResponseContext(200, {"results": self.task_lists_by_parent.get(parent_id, [])})
        item_id = parsed.path.rsplit("/", 1)[-1]
        status = self.task_statuses.get(item_id)
        if status is not None:
            data = self.task_contexts.get(item_id, {})
            return JsonResponseContext(status, data)
        data = self.task_contexts.get(item_id)
        if data is not None:
            return JsonResponseContext(200, data)
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


def _request(
    proxy,
    payload: dict[str, Any],
    session: RecordingSession | None = None,
    headers: dict[str, str] | None = None,
) -> StubRequest:
    body = _body(payload)
    secret = b"test-secret"
    return StubRequest(
        body=body,
        signature=_signature(secret, body),
        app={"secret": secret, "session": session or RecordingSession(), "todoist_api_key": "test-api-key"},
        extra_headers=headers,
    )


def _ledger_rows(db_path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


def _ledger_count(db_path: Path, table: str) -> int:
    return _ledger_rows(db_path, f"SELECT COUNT(*) FROM {table}")[0][0]


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


def _write_conditional_routes(todoist_proxy_fixture: TodoistProxyFixture) -> None:
    routing = {
        "routes": {
            LOWKEYCODES_PROJECT_ID: {
                "max-lowkeycodes": {
                    "agent": "max",
                    "responsible_uids": ["59328091"],
                    "section_ids": [SECTION_MAX],
                    "creator_uids": ["59328091"],
                    "mention_aliases": ["@Max", "Max", "Max | CEO"],
                },
                "abra-lowkeycodes": {
                    "agent": "abra",
                    "responsible_uids": ["15795569"],
                    "section_ids": [SECTION_ABRA],
                    "creator_uids": ["15795569"],
                    "mention_aliases": ["@Abra", "Abra", "Abra | CMO"],
                },
                "smith-lowkeycodes": {
                    "agent": "smith",
                    "responsible_uids": ["29584133"],
                    "section_ids": [SECTION_SMITH],
                    "creator_uids": ["29584133"],
                    "mention_aliases": ["@Smith", "Smith", "Smith | DevOps"],
                },
            }
        },
        "upstreams": {
            "max-lowkeycodes": "http://127.0.0.1:8644",
            "abra-lowkeycodes": "http://127.0.0.1:8644",
            "smith-lowkeycodes": "http://127.0.0.1:8644",
        },
    }
    todoist_proxy_fixture.routing_file.write_text(json.dumps(routing, indent=2, sort_keys=True) + "\n")


def _pending_subscriptions(db_path: Path) -> list[str]:
    return [
        row[0]
        for row in _ledger_rows(
            db_path,
            """
            SELECT subscription
            FROM pending_deliveries
            WHERE kind = 'delivery'
            ORDER BY subscription
            """,
        )
    ]


def _pending_rows(db_path: Path) -> list[tuple[Any, ...]]:
    return _ledger_rows(
        db_path,
        """
        SELECT kind, subscription, state, attempt_count, last_error
        FROM pending_deliveries
        ORDER BY id
        """,
    )


def _inbound_payload(db_path: Path) -> dict[str, Any]:
    raw_body = _ledger_rows(db_path, "SELECT raw_body FROM inbound_events ORDER BY id DESC LIMIT 1")[0][0]
    return json.loads(raw_body)


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


def test_malformed_signed_json_never_records_payload(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    body = b'{"event_name":"item:added"'
    secret = b"test-secret"
    session = RecordingSession()
    request = StubRequest(
        body=body,
        signature=_signature(secret, body),
        app={"secret": secret, "session": session, "todoist_api_key": "test-api-key"},
    )

    response = asyncio.run(proxy.handle(request))

    assert response.status == 400
    assert response.text == "invalid JSON"
    assert session.urls == []
    assert _ledger_rows(todoist_proxy_fixture.interaction_db_file, "SELECT name FROM sqlite_master") == []


def test_oversized_body_rejected_before_signature_or_ledger(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    session = RecordingSession()
    request = StubRequest(
        body=b"x" * (proxy.MAX_BODY + 1),
        signature="not-checked",
        app={"secret": b"test-secret", "session": session, "todoist_api_key": "test-api-key"},
    )

    response = asyncio.run(proxy.handle(request))

    assert response.status == 413
    assert response.text == "payload too large"
    assert session.urls == []
    assert _ledger_rows(todoist_proxy_fixture.interaction_db_file, "SELECT name FROM sqlite_master") == []


def test_non_webhook_request_returns_404_without_ledger(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    session = RecordingSession()
    request = StubRequest(
        body=b"{}",
        signature="not-checked",
        app={"secret": b"test-secret", "session": session, "todoist_api_key": "test-api-key"},
        method="GET",
        path="/webhooks/todoist",
    )

    response = asyncio.run(proxy.handle(request))

    assert response.status == 404
    assert response.text == "not found"
    assert session.urls == []
    assert _ledger_rows(todoist_proxy_fixture.interaction_db_file, "SELECT name FROM sqlite_master") == []


def test_valid_signed_payload_records_event_and_queues_without_forwarding(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    payload = todoist_proxy_fixture.payloads["item_added"]
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert response.text == "ok"
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == [
        "abra-lowkeycodes",
        "max-lowkeycodes",
        "smith-lowkeycodes",
    ]
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
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT event_name, entity_id, project_id, status FROM inbound_events",
    ) == [("item:added", payload["event_data"]["id"], payload["event_data"]["project_id"], "accepted")]
    assert routing_rows == [
        ("abra-lowkeycodes", 1, "forwarding_enabled"),
        ("max-lowkeycodes", 1, "forwarding_enabled"),
        ("smith-lowkeycodes", 1, "forwarding_enabled"),
    ]
    assert interaction_rows == []


def test_parent_task_context_packet_is_persisted_for_delivery(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["item_added"])
    payload["event_data"].update(
        {
            "id": "task-child-smith-001",
            "content": "[owner: smith] Validate parent decision",
            "parent_id": "task-parent-max-001",
            "responsible_uid": "29584133",
            "section_id": SECTION_SMITH,
        }
    )
    session = RecordingSession(
        task_contexts={
            "task-parent-max-001": {
                "id": "task-parent-max-001",
                "content": "Max parent decision task",
                "project_id": LOWKEYCODES_PROJECT_ID,
                "responsible_uid": "59328091",
            }
        },
        task_lists_by_parent={
            "task-parent-max-001": [
                {
                    "id": "task-child-smith-001",
                    "content": "[owner: smith] Validate parent decision",
                    "parent_id": "task-parent-max-001",
                },
                {
                    "id": "task-child-abra-001",
                    "content": "[owner: abra] Review parent positioning",
                    "parent_id": "task-parent-max-001",
                },
            ],
            "task-child-smith-001": [],
        },
    )

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == ["smith-lowkeycodes"]
    persisted_payload = _inbound_payload(todoist_proxy_fixture.interaction_db_file)
    packet = persisted_payload["event_data"]["context_packet"]
    assert packet["status"] == "ok"
    assert packet["task_tree"]["parent_task_id"] == "task-parent-max-001"
    assert packet["task_tree"]["parent_task"]["content"] == "Max parent decision task"
    assert packet["task_tree"]["siblings"] == [
        {
            "id": "task-child-abra-001",
            "content": "[owner: abra] Review parent positioning",
            "parent_id": "task-parent-max-001",
        }
    ]
    assert "subtask" in packet["summary"]


def test_routed_enqueue_failure_returns_503_without_forwarding_or_pending(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch,
) -> None:
    proxy = _module()
    payload = todoist_proxy_fixture.payloads["item_added"]
    session = RecordingSession()

    def fail_enqueue(*args: Any, **kwargs: Any):
        return proxy.LedgerResult(success=False, reason="sqlite_error", error="boom")

    monkeypatch.setattr(
        proxy.ControlLedger,
        "record_inbound_event_and_enqueue_pending_deliveries",
        fail_enqueue,
    )

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 503
    assert response.text == "pending delivery persistence failed"
    assert session.urls == []
    assert _ledger_count(todoist_proxy_fixture.interaction_db_file, "events") == 0
    assert _ledger_count(todoist_proxy_fixture.interaction_db_file, "inbound_events") == 0
    assert _ledger_count(todoist_proxy_fixture.interaction_db_file, "pending_deliveries") == 0
    assert _ledger_count(todoist_proxy_fixture.interaction_db_file, "routing_decisions") == 0
    assert _ledger_count(todoist_proxy_fixture.interaction_db_file, "interactions") == 0


def test_conditional_max_assigned_task_only_posts_max(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["item_added"])
    payload["event_data"].update({"responsible_uid": "59328091", "section_id": SECTION_SMITH})
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == ["max-lowkeycodes"]
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [("max-lowkeycodes", 1, "forwarding_enabled")]


def test_conditional_unassigned_smith_section_task_only_posts_smith(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["item_added"])
    payload["event_data"].update({"responsible_uid": None, "section_id": SECTION_SMITH})
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == ["smith-lowkeycodes"]
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [("smith-lowkeycodes", 1, "forwarding_enabled")]


def test_conditional_assigned_abra_in_smith_section_posts_abra_not_smith(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["item_added"])
    payload["event_data"].update({"responsible_uid": "15795569", "section_id": SECTION_SMITH})
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == ["abra-lowkeycodes"]
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [("abra-lowkeycodes", 1, "forwarding_enabled")]


def test_conditional_lifecycle_event_can_post_assignee_and_creator(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["item_added"])
    payload["event_name"] = "item:completed"
    payload["event_data"].update(
        {
            "id": "task-lifecycle-max-smith-001",
            "responsible_uid": "59328091",
            "creator_uid": "29584133",
            "section_id": SECTION_ABRA,
        }
    )
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == [
        "max-lowkeycodes",
        "smith-lowkeycodes",
    ]
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [
        ("max-lowkeycodes", 1, "forwarding_enabled"),
        ("smith-lowkeycodes", 1, "forwarding_enabled"),
    ]


def test_conditional_note_explicit_mention_routes_only_mentioned_agent(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-explicit-max-001",
            "content": "@Max please review this even though another agent owns the task",
            "item_id": "task-parent-smith-001",
            "posted_uid": "29584133",
        }
    )
    session = RecordingSession(
        task_contexts={
            payload["event_data"]["item_id"]: {
                "project_id": LOWKEYCODES_PROJECT_ID,
                "responsible_uid": "29584133",
                "section_id": SECTION_SMITH,
            }
        }
    )

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == ["max-lowkeycodes"]
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [("max-lowkeycodes", 1, "forwarding_enabled")]


def test_conditional_note_without_mention_routes_by_parent_assignee(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-parent-max-001",
            "content": "Please review the latest deployment note",
            "item_id": "task-parent-max-001",
            "posted_uid": "29584133",
        }
    )
    session = RecordingSession(
        task_contexts={
            payload["event_data"]["item_id"]: {
                "project_id": LOWKEYCODES_PROJECT_ID,
                "responsible_uid": "59328091",
                "section_id": SECTION_SMITH,
            }
        }
    )

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == ["max-lowkeycodes"]
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [("max-lowkeycodes", 1, "forwarding_enabled")]


def test_conditional_note_without_mention_lookup_404_fails_closed(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-missing-parent-001",
            "content": "No explicit routed mention here",
            "item_id": "task-deleted-parent-001",
            "posted_uid": "29584133",
        }
    )
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert response.text == "no route"
    assert session.urls == []
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == []


def test_conditional_note_retryable_parent_lookup_queues_routing_resolution(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-parent-503-001",
            "content": "No explicit routed mention here",
            "item_id": "task-parent-503-001",
            "posted_uid": "29584133",
        }
    )
    session = RecordingSession(task_statuses={payload["event_data"]["item_id"]: 503})

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert response.text == "ok"
    assert session.urls == []
    assert len(session.get_urls) == 1
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT event_name, entity_id, project_id, status FROM inbound_events",
    ) == [("note:added", payload["event_data"]["id"], "", "accepted")]
    assert _pending_rows(todoist_proxy_fixture.interaction_db_file) == [
        ("routing_resolution", None, "pending", 0, None)
    ]
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == []


def test_project_level_note_explicit_mention_routes_without_parent_lookup(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"] = {
        "id": "project-comment-smith-001",
        "content": "@Smith please check the project-level update",
        "project_id": LOWKEYCODES_PROJECT_ID,
        "posted_uid": "15611160",
    }
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert session.get_urls == []
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == ["smith-lowkeycodes"]
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [("smith-lowkeycodes", 1, "forwarding_enabled")]


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
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT event_name, entity_id, project_id, status FROM inbound_events",
    ) == [
        ("item:added", payload["event_data"]["id"], payload["event_data"]["project_id"], "accepted")
    ]
    assert _ledger_count(todoist_proxy_fixture.interaction_db_file, "pending_deliveries") == 0
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
        "SELECT event_name, entity_id, project_id, status FROM inbound_events",
    ) == [("item:added", "task-unrouted-001", UNKNOWN_PROJECT_ID, "accepted")]
    assert _ledger_count(todoist_proxy_fixture.interaction_db_file, "pending_deliveries") == 0
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT interaction_type, project_id, status, reason FROM interactions",
    ) == [("routing", UNKNOWN_PROJECT_ID, "unrouted", "no_route")]


def test_disabled_valid_event_records_inbound_and_suppressed_without_pending_or_forwarding(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.disable_file.touch()
    proxy = _module()
    payload = todoist_proxy_fixture.payloads["item_added"]
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert response.text == "proxy disabled"
    assert session.urls == []
    assert session.get_urls == []
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT event_name, entity_id, project_id, status FROM inbound_events",
    ) == [
        ("item:added", payload["event_data"]["id"], payload["event_data"]["project_id"], "accepted")
    ]
    assert _ledger_count(todoist_proxy_fixture.interaction_db_file, "pending_deliveries") == 0
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions",
    ) == [("", 0, "legacy_disable_sentinel_present")]
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT interaction_type, agent, project_id, status, reason FROM interactions",
    ) == [
        (
            "forward",
            "",
            payload["event_data"]["project_id"],
            "suppressed",
            "legacy_disable_sentinel_present",
        )
    ]


def test_disabled_note_added_without_project_records_inbound_without_parent_lookup(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    todoist_proxy_fixture.disable_file.touch()
    proxy = _module()
    payload = todoist_proxy_fixture.payloads["note_added"]
    session = RecordingSession(
        task_project_ids={payload["event_data"]["item_id"]: LOWKEYCODES_PROJECT_ID}
    )

    response = asyncio.run(proxy.handle(_request(proxy, payload, session)))

    assert response.status == 200
    assert session.urls == []
    assert session.get_urls == []
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT event_name, entity_id, project_id, status FROM inbound_events",
    ) == [("note:added", payload["event_data"]["id"], "", "accepted")]
    assert _ledger_count(todoist_proxy_fixture.interaction_db_file, "pending_deliveries") == 0
    assert _ledger_rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT interaction_type, status, reason FROM interactions",
    ) == [("forward", "suppressed", "legacy_disable_sentinel_present")]
