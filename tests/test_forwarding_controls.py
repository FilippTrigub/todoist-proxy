"""Proxy forwarding controls for per-target suppression and retry semantics."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from conftest import LOWKEYCODES_PROJECT_ID, TodoistProxyFixture
from test_proxy_webhook import (
    RecordingSession,
    _pending_rows,
    _pending_subscriptions,
    _request,
    _write_conditional_routes,
)


def _module():
    return importlib.reload(importlib.import_module("proxy"))


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def _rows(db_path: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


def _payload_copy(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload))


def _single_max_payload(todoist_proxy_fixture: TodoistProxyFixture) -> dict[str, Any]:
    payload = _payload_copy(todoist_proxy_fixture.payloads["item_added"])
    payload["event_data"].update({"responsible_uid": "59328091", "section_id": "6gpFcCxmc39r8MrQ"})
    return payload


class HeaderRecordingSession(RecordingSession):
    def __init__(self, statuses: dict[str, int] | None = None) -> None:
        super().__init__(statuses)
        self.post_kwargs: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.post_kwargs.append(kwargs)
        return super().post(url, **kwargs)


def test_normal_fanout_records_one_routing_decision_per_delivery_target(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session)))

    assert response.status == 200
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == [
        "abra-lowkeycodes",
        "max-lowkeycodes",
        "smith-lowkeycodes",
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [
        ("abra-lowkeycodes", 1, "forwarding_enabled"),
        ("max-lowkeycodes", 1, "forwarding_enabled"),
        ("smith-lowkeycodes", 1, "forwarding_enabled"),
    ]


def test_agent_disabled_suppresses_only_that_target_and_forwards_remaining(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_config(todoist_proxy_fixture.control_config_file, {"agents": {"abra": {"enabled": False}}})
    proxy = _module()
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session)))

    assert response.status == 200
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == [
        "max-lowkeycodes",
        "smith-lowkeycodes",
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [
        ("abra-lowkeycodes", 0, "agent_disabled:abra"),
        ("max-lowkeycodes", 1, "forwarding_enabled"),
        ("smith-lowkeycodes", 1, "forwarding_enabled"),
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT agent, status, reason FROM interactions WHERE interaction_type = 'forward' ORDER BY agent",
    ) == [("abra", "suppressed", "agent_disabled:abra")]


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


def test_disabled_targets_do_not_mask_enabled_queueing(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_config(todoist_proxy_fixture.control_config_file, {"agents": {"abra": {"enabled": False}}})
    proxy = _module()
    session = RecordingSession({"max-lowkeycodes": 503, "smith-lowkeycodes": 504})

    response = asyncio.run(proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session)))

    assert response.status == 200
    assert response.text == "ok"
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == [
        "max-lowkeycodes",
        "smith-lowkeycodes",
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [
        ("abra-lowkeycodes", 0, "agent_disabled:abra"),
        ("max-lowkeycodes", 1, "forwarding_enabled"),
        ("smith-lowkeycodes", 1, "forwarding_enabled"),
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT agent, status, reason FROM interactions WHERE interaction_type = 'forward' ORDER BY agent",
    ) == [("abra", "suppressed", "agent_disabled:abra")]


def test_downstream_statuses_are_not_observed_before_ack(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_config(todoist_proxy_fixture.control_config_file, {"agents": {"abra": {"enabled": False}}})
    proxy = _module()
    session = RecordingSession({"max-lowkeycodes": 503, "smith-lowkeycodes": 200})

    response = asyncio.run(proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session)))

    assert response.status == 200
    assert response.text == "ok"
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == [
        "max-lowkeycodes",
        "smith-lowkeycodes",
    ]


def test_disabled_conditional_target_records_only_matched_agent_gate(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    _write_config(todoist_proxy_fixture.control_config_file, {"agents": {"max": {"enabled": False}}})
    proxy = _module()
    session = RecordingSession()

    response = asyncio.run(proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session)))

    assert response.status == 200
    assert session.urls == []
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions ORDER BY target",
    ) == [("max-lowkeycodes", 0, "agent_disabled:max")]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT agent, status, reason FROM interactions WHERE interaction_type = 'forward'",
    ) == [("max", "suppressed", "agent_disabled:max")]


def test_successful_delivery_id_skips_already_successful_targets(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    delivery_headers = {"X-Todoist-Delivery-ID": "delivery-duplicate-001"}
    ledger = proxy.ControlLedger()
    assert ledger.initialize_schema().success
    for subscription in ("abra-lowkeycodes", "max-lowkeycodes", "smith-lowkeycodes"):
        assert ledger.record_successful_delivery(
            source="todoist",
            event_name=todoist_proxy_fixture.payloads["item_added"]["event_name"],
            event_data=todoist_proxy_fixture.payloads["item_added"]["event_data"],
            subscription=subscription,
            delivery_id=delivery_headers["X-Todoist-Delivery-ID"],
            payload=todoist_proxy_fixture.payloads["item_added"],
        ).success
    session = RecordingSession()

    response = asyncio.run(
        proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session, delivery_headers))
    )

    assert response.status == 200
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == []
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription, delivery_id FROM delivery_dedup ORDER BY subscription",
    ) == [
        ("abra-lowkeycodes", "delivery-duplicate-001"),
        ("max-lowkeycodes", "delivery-duplicate-001"),
        ("smith-lowkeycodes", "delivery-duplicate-001"),
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        """
        SELECT agent, status, reason
        FROM interactions
        WHERE interaction_type = 'forward' AND status = 'skipped'
        ORDER BY agent
        """,
    ) == [
        ("abra", "skipped", "already_delivered"),
        ("max", "skipped", "already_delivered"),
        ("smith", "skipped", "already_delivered"),
    ]


def test_already_successful_partial_fanout_queues_only_missing_target(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    delivery_headers = {"X-Todoist-Delivery-ID": "delivery-partial-001"}
    ledger = proxy.ControlLedger()
    assert ledger.initialize_schema().success
    for subscription in ("max-lowkeycodes", "smith-lowkeycodes"):
        assert ledger.record_successful_delivery(
            source="todoist",
            event_name=todoist_proxy_fixture.payloads["item_added"]["event_name"],
            event_data=todoist_proxy_fixture.payloads["item_added"]["event_data"],
            subscription=subscription,
            delivery_id=delivery_headers["X-Todoist-Delivery-ID"],
            payload=todoist_proxy_fixture.payloads["item_added"],
        ).success
    session = RecordingSession({"abra-lowkeycodes": 200, "max-lowkeycodes": 200, "smith-lowkeycodes": 200})

    response = asyncio.run(
        proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], session, delivery_headers))
    )

    assert response.status == 200
    assert response.text == "ok"
    assert session.urls == []
    assert _pending_subscriptions(todoist_proxy_fixture.interaction_db_file) == ["abra-lowkeycodes"]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription, delivery_id FROM delivery_dedup ORDER BY subscription",
    ) == [
        ("max-lowkeycodes", "delivery-partial-001"),
        ("smith-lowkeycodes", "delivery-partial-001"),
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        """
        SELECT agent, status, reason
        FROM interactions
        WHERE interaction_type = 'forward'
        ORDER BY id
        """,
    ) == [
        ("max", "skipped", "already_delivered"),
        ("smith", "skipped", "already_delivered"),
    ]


def test_drain_success_marks_pending_succeeded_and_records_delivery_dedup(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    delivery_headers = {"X-Todoist-Delivery-ID": "delivery-drain-success-001"}
    handle_session = RecordingSession()
    response = asyncio.run(
        proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], handle_session, delivery_headers))
    )
    drain_session = RecordingSession()

    result = asyncio.run(proxy.drain_pending_deliveries(drain_session))

    assert response.status == 200
    assert handle_session.urls == []
    assert result == {"attempted": 3, "succeeded": 3, "retry": 0, "terminal_failed": 0, "skipped": 0}
    assert [url.rsplit("/", 1)[-1] for url in drain_session.urls] == [
        "max-lowkeycodes",
        "abra-lowkeycodes",
        "smith-lowkeycodes",
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription, state, attempt_count FROM pending_deliveries ORDER BY subscription",
    ) == [
        ("abra-lowkeycodes", "succeeded", 0),
        ("max-lowkeycodes", "succeeded", 0),
        ("smith-lowkeycodes", "succeeded", 0),
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription, delivery_id FROM delivery_dedup ORDER BY subscription",
    ) == [
        ("abra-lowkeycodes", "delivery-drain-success-001"),
        ("max-lowkeycodes", "delivery-drain-success-001"),
        ("smith-lowkeycodes", "delivery-drain-success-001"),
    ]


def test_drain_uses_default_upstream_and_reconstructs_forward_headers(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    routing = json.loads(todoist_proxy_fixture.routing_file.read_text())
    routing["upstreams"].pop("hausmeister-inbox")
    todoist_proxy_fixture.routing_file.write_text(json.dumps(routing, indent=2, sort_keys=True) + "\n")
    proxy = _module()
    delivery_headers = {
        "Content-Type": "application/json",
        "X-Todoist-Delivery-ID": "delivery-drain-default-upstream-001",
    }
    response = asyncio.run(
        proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["due_poll_item_added"], RecordingSession(), delivery_headers))
    )
    drain_session = HeaderRecordingSession()

    result = asyncio.run(proxy.drain_pending_deliveries(drain_session))

    assert response.status == 200
    assert result == {"attempted": 1, "succeeded": 1, "retry": 0, "terminal_failed": 0, "skipped": 0}
    assert drain_session.urls == ["http://127.0.0.1:8644/webhooks/hausmeister-inbox"]
    assert drain_session.post_kwargs[0]["headers"] == {
        "Content-Type": "application/json",
        "X-GitHub-Event": "item:added",
        "X-Todoist-Delivery-ID": "delivery-drain-default-upstream-001",
        "X-Todoist-Hmac-SHA256": drain_session.post_kwargs[0]["headers"]["X-Todoist-Hmac-SHA256"],
    }


def test_drain_5xx_marks_retry_and_does_not_record_delivery_dedup(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _single_max_payload(todoist_proxy_fixture)
    response = asyncio.run(proxy.handle(_request(proxy, payload, RecordingSession())))
    drain_session = RecordingSession({"max-lowkeycodes": 503})

    result = asyncio.run(
        proxy.drain_pending_deliveries(
            drain_session,
            now="2100-01-01T00:00:00+00:00",
            retry_delay_seconds=60,
        )
    )

    assert response.status == 200
    assert result == {"attempted": 1, "succeeded": 0, "retry": 1, "terminal_failed": 0, "skipped": 0}
    assert [url.rsplit("/", 1)[-1] for url in drain_session.urls] == ["max-lowkeycodes"]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription, state, attempt_count, next_attempt_at, last_error FROM pending_deliveries",
    ) == [("max-lowkeycodes", "retry", 1, "2100-01-01T00:01:00+00:00", "http_503")]
    assert _rows(todoist_proxy_fixture.interaction_db_file, "SELECT subscription FROM delivery_dedup") == []


def test_drain_4xx_marks_terminal_and_is_not_retried(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _single_max_payload(todoist_proxy_fixture)
    response = asyncio.run(proxy.handle(_request(proxy, payload, RecordingSession())))
    drain_session = RecordingSession({"max-lowkeycodes": 404})

    first = asyncio.run(proxy.drain_pending_deliveries(drain_session))
    second = asyncio.run(proxy.drain_pending_deliveries(drain_session))

    assert response.status == 200
    assert first == {"attempted": 1, "succeeded": 0, "retry": 0, "terminal_failed": 1, "skipped": 0}
    assert second == {"attempted": 0, "succeeded": 0, "retry": 0, "terminal_failed": 0, "skipped": 0}
    assert [url.rsplit("/", 1)[-1] for url in drain_session.urls] == ["max-lowkeycodes"]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription, state, attempt_count, last_error FROM pending_deliveries",
    ) == [("max-lowkeycodes", "terminal_failed", 0, "http_404")]
    assert _rows(todoist_proxy_fixture.interaction_db_file, "SELECT subscription FROM delivery_dedup") == []


def test_drain_partial_fanout_retries_only_failed_target(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    proxy = _module()
    delivery_headers = {"X-Todoist-Delivery-ID": "delivery-drain-partial-001"}
    response = asyncio.run(
        proxy.handle(_request(proxy, todoist_proxy_fixture.payloads["item_added"], RecordingSession(), delivery_headers))
    )
    first_session = RecordingSession({"abra-lowkeycodes": 503})

    first = asyncio.run(
        proxy.drain_pending_deliveries(
            first_session,
            now="2100-01-01T00:00:00+00:00",
            retry_delay_seconds=60,
        )
    )
    second_session = RecordingSession({"abra-lowkeycodes": 200})
    second = asyncio.run(
        proxy.drain_pending_deliveries(second_session, now="2100-01-01T00:01:00+00:00")
    )

    assert response.status == 200
    assert first == {"attempted": 3, "succeeded": 2, "retry": 1, "terminal_failed": 0, "skipped": 0}
    assert second == {"attempted": 1, "succeeded": 1, "retry": 0, "terminal_failed": 0, "skipped": 0}
    assert [url.rsplit("/", 1)[-1] for url in first_session.urls] == [
        "max-lowkeycodes",
        "abra-lowkeycodes",
        "smith-lowkeycodes",
    ]
    assert [url.rsplit("/", 1)[-1] for url in second_session.urls] == ["abra-lowkeycodes"]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription, state, attempt_count FROM pending_deliveries ORDER BY subscription",
    ) == [
        ("abra-lowkeycodes", "succeeded", 1),
        ("max-lowkeycodes", "succeeded", 0),
        ("smith-lowkeycodes", "succeeded", 0),
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription FROM delivery_dedup ORDER BY subscription",
    ) == [("abra-lowkeycodes",), ("max-lowkeycodes",), ("smith-lowkeycodes",)]


def test_drain_skips_pending_row_when_delivery_id_already_succeeded(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _single_max_payload(todoist_proxy_fixture)
    delivery_id = "delivery-drain-duplicate-001"
    response = asyncio.run(
        proxy.handle(_request(proxy, payload, RecordingSession(), {"X-Todoist-Delivery-ID": delivery_id}))
    )
    ledger = proxy.ControlLedger()
    assert ledger.record_successful_delivery(
        source="todoist",
        event_name=payload["event_name"],
        event_data=payload["event_data"],
        subscription="max-lowkeycodes",
        delivery_id=delivery_id,
        payload=payload,
    ).success
    drain_session = RecordingSession()

    result = asyncio.run(proxy.drain_pending_deliveries(drain_session))

    assert response.status == 200
    assert result == {"attempted": 0, "succeeded": 0, "retry": 0, "terminal_failed": 0, "skipped": 1}
    assert drain_session.urls == []
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT subscription, state FROM pending_deliveries",
    ) == [("max-lowkeycodes", "succeeded")]


def test_drain_routing_resolution_success_creates_and_sends_delivery(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-resolution-success-001",
            "content": "Please review this parent-owned note",
            "item_id": "task-parent-max-resolution-001",
            "posted_uid": "29584133",
        }
    )
    handle_session = RecordingSession(task_statuses={payload["event_data"]["item_id"]: 503})
    response = asyncio.run(proxy.handle(_request(proxy, payload, handle_session)))
    drain_session = RecordingSession(
        task_contexts={
            payload["event_data"]["item_id"]: {
                "project_id": LOWKEYCODES_PROJECT_ID,
                "responsible_uid": "59328091",
                "section_id": "6gpFcCxmc39r8MrQ",
            }
        }
    )

    result = asyncio.run(proxy.drain_pending_deliveries(drain_session))

    assert response.status == 200
    assert response.text == "ok"
    assert handle_session.urls == []
    assert result == {"attempted": 1, "succeeded": 1, "retry": 0, "terminal_failed": 0, "skipped": 0}
    assert [url.rsplit("/", 1)[-1] for url in drain_session.urls] == ["max-lowkeycodes"]
    assert _pending_rows(todoist_proxy_fixture.interaction_db_file) == [
        ("routing_resolution", None, "succeeded", 0, None),
        ("delivery", "max-lowkeycodes", "succeeded", 0, None),
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT target, enabled, reason FROM routing_decisions",
    ) == [("max-lowkeycodes", 1, "forwarding_enabled")]


def test_drain_routing_resolution_retryable_lookup_backs_off(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-resolution-retry-001",
            "content": "Please review this parent-owned note",
            "item_id": "task-parent-retry-resolution-001",
            "posted_uid": "29584133",
        }
    )
    handle_session = RecordingSession(task_statuses={payload["event_data"]["item_id"]: 503})
    response = asyncio.run(proxy.handle(_request(proxy, payload, handle_session)))
    drain_session = RecordingSession(task_statuses={payload["event_data"]["item_id"]: 503})

    result = asyncio.run(
        proxy.drain_pending_deliveries(
            drain_session,
            now="2100-01-01T00:00:00+00:00",
            retry_delay_seconds=60,
        )
    )

    assert response.status == 200
    assert result == {"attempted": 0, "succeeded": 0, "retry": 1, "terminal_failed": 0, "skipped": 0}
    assert drain_session.urls == []
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        "SELECT kind, subscription, state, attempt_count, next_attempt_at, last_error FROM pending_deliveries",
    ) == [
        (
            "routing_resolution",
            None,
            "retry",
            1,
            "2100-01-01T00:01:00+00:00",
            "task_lookup_retryable",
        )
    ]


def test_drain_routing_resolution_terminal_failure_records_audit_reason(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    _write_conditional_routes(todoist_proxy_fixture)
    proxy = _module()
    payload = _payload_copy(todoist_proxy_fixture.payloads["note_added"])
    payload["event_data"].update(
        {
            "id": "comment-resolution-terminal-001",
            "content": "Please review this parent-owned note",
            "item_id": "task-parent-terminal-resolution-001",
            "posted_uid": "29584133",
        }
    )
    handle_session = RecordingSession(task_statuses={payload["event_data"]["item_id"]: 503})
    response = asyncio.run(proxy.handle(_request(proxy, payload, handle_session)))
    drain_session = RecordingSession()

    result = asyncio.run(proxy.drain_pending_deliveries(drain_session))

    assert response.status == 200
    assert result == {"attempted": 0, "succeeded": 0, "retry": 0, "terminal_failed": 1, "skipped": 0}
    assert drain_session.urls == []
    assert _pending_rows(todoist_proxy_fixture.interaction_db_file) == [
        ("routing_resolution", None, "terminal_failed", 0, "no_route_after_resolution")
    ]
    assert _rows(
        todoist_proxy_fixture.interaction_db_file,
        """
        SELECT interaction_type, status, reason
        FROM interactions
        WHERE interaction_type = 'routing'
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == [("routing", "terminal_failed", "no_route_after_resolution")]
