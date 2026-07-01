#!/usr/bin/env python3
"""
Todoist → Hermes webhook router proxy.

Flow:
  1. Validate X-Todoist-Hmac-SHA256 (base64 HMAC-SHA256 using the OAuth
     app's client_secret). Reject with 401 on mismatch or missing header.
  2. For item:added with a due date in the future, drop it here (200, no
     forward) — due_poller.py will deliver an equivalent event once the
     task is actually due. Without this, every agent reacts to a task the
     moment it's created, regardless of when it's actually meant to start.
  3. Extract event_data.project_id from the payload.
  4. Load ~/.hermes/todoist-routing.json and look up which Hermes
     subscriptions handle that project.
   5. Durably enqueue each matching subscription delivery.
   6. Return 200 to Todoist after local persistence; downstream delivery is
      drained later from SQLite.

Routing config (~/.hermes/todoist-routing.json) maps project IDs to lists
of Hermes subscription names. It is re-read on every request so adding or
changing routes takes effect immediately without a proxy restart:

  {
    "6ggFh66x4WXVVqGH": ["hausmeister-inbox"],
    "6gmpjVFv2wVG7XJQ": ["max-lowkeycodes", "abra-lowkeycodes", "smith-lowkeycodes"]
  }

The Todoist webhook URL path does not affect routing — only the project_id
in the payload does. A single Todoist webhook registration is sufficient.

Also serves GET /oauth/callback for the one-time OAuth authorization that
activates webhook delivery for an account.

Required env var : TODOIST_CLIENT_SECRET
Optional env vars: TODOIST_CLIENT_ID     (needed for /oauth/callback)
                   TODOIST_ROUTING_FILE  (default: ~/.hermes/todoist-routing.json)
                   PROXY_PORT            (default: 8645)
"""
import base64
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from aiohttp import ClientSession, web

from control_ledger import ControlLedger, LedgerResult, evaluate_forwarding
from due_utils import due_status
from interaction_extractor import extract_interactions
from route_matcher import match_routes

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8645"))
DRAIN_INTERVAL_SECONDS = float(os.environ.get("TODOIST_DRAIN_INTERVAL_SECONDS", "2"))
MAX_BODY = 1 * 1024 * 1024  # 1 MB
TODOIST_SIG_HEADER = "X-Todoist-Hmac-SHA256"
OAUTH_TOKEN_URL = "https://todoist.com/oauth/access_token"
TODOIST_TASKS_URL = "https://api.todoist.com/api/v1/tasks"
PUBLIC_BASE = "https://the-data-server.tailf73bbe.ts.net"
DEFAULT_UPSTREAM = "http://127.0.0.1:8644"
RETRYABLE_TASK_LOOKUP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
TASK_CONTEXT_FIELDS = (
    "project_id",
    "section_id",
    "responsible_uid",
    "assignee_id",
    "added_by_uid",
    "creator_uid",
    "creator_id",
)
TASK_SUMMARY_FIELDS = (
    "id",
    "content",
    "description",
    "project_id",
    "section_id",
    "parent_id",
    "responsible_uid",
    "assignee_id",
    "added_by_uid",
    "creator_uid",
    "creator_id",
    "priority",
    "labels",
    "due",
    "url",
    "checked",
    "completed_at",
)
ROUTING_FILE = Path(
    os.environ.get(
        "TODOIST_ROUTING_FILE",
        Path.home() / ".hermes" / "todoist-routing.json",
    )
)
DISABLE_FILE = Path(
    os.environ.get(
        "TODOIST_DISABLE_FILE",
        Path.home() / ".hermes" / "todoist-proxy.disabled",
    )
)

SUBSCRIPTION_AGENT_MAP = {
    "max-lowkeycodes": "max",
    "abra-lowkeycodes": "abra",
    "smith-lowkeycodes": "smith",
    "hausmeister-inbox": "hausmeister",
}


@dataclass(frozen=True)
class TaskContextResult:
    data: dict[str, Any]
    retryable_failure: bool = False
    status: int = 0
    error: str = ""

    @property
    def project_id(self) -> str:
        return str(self.data.get("project_id", "") or "")


def _opaque_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text if text else ""


def _first_opaque_id(data: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = _opaque_id(data.get(name))
        if value:
            return value
    return ""


def _task_context_from_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    context = {
        "project_id": _first_opaque_id(data, "project_id", "projectId"),
        "section_id": _first_opaque_id(data, "section_id", "sectionId"),
        "responsible_uid": _first_opaque_id(data, "responsible_uid", "responsibleUid"),
        "assignee_id": _first_opaque_id(data, "assignee_id", "assigneeId"),
        "added_by_uid": _first_opaque_id(data, "added_by_uid", "addedByUid"),
        "creator_uid": _first_opaque_id(data, "creator_uid", "creatorUid"),
        "creator_id": _first_opaque_id(data, "creator_id", "creatorId"),
    }
    return {key: value for key, value in context.items() if value}


def _task_summary_from_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for field in TASK_SUMMARY_FIELDS:
        value = data.get(field)
        if value is None or value == "":
            continue
        summary[field] = value

    camel_parent = data.get("parentId")
    if camel_parent and "parent_id" not in summary:
        summary["parent_id"] = camel_parent
    camel_project = data.get("projectId")
    if camel_project and "project_id" not in summary:
        summary["project_id"] = camel_project
    camel_section = data.get("sectionId")
    if camel_section and "section_id" not in summary:
        summary["section_id"] = camel_section
    return summary


async def _lookup_task_summary(
    session: ClientSession,
    task_id: str,
    api_key: str,
) -> TaskContextResult:
    if not task_id or not api_key:
        return TaskContextResult({})
    try:
        async with session.get(
            f"{TODOIST_TASKS_URL}/{quote(task_id, safe='')}",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, Mapping):
                    return TaskContextResult(_task_summary_from_mapping(data), status=resp.status)
                log.warning("task summary lookup %s returned non-object JSON", task_id)
                return TaskContextResult({}, status=resp.status)
            retryable = resp.status in RETRYABLE_TASK_LOOKUP_STATUSES or resp.status >= 500
            log.warning("task summary lookup %s returned %d", task_id, resp.status)
            return TaskContextResult({}, retryable_failure=retryable, status=resp.status)
    except TimeoutError as exc:
        log.warning("task summary lookup %s timeout: %s", task_id, exc)
        return TaskContextResult({}, retryable_failure=True, error=str(exc))
    except Exception as exc:
        log.warning("task summary lookup %s error: %s", task_id, exc)
        return TaskContextResult({}, retryable_failure=True, error=str(exc))


async def _lookup_child_task_summaries(
    session: ClientSession,
    parent_id: str,
    api_key: str,
    *,
    limit: int = 8,
) -> TaskContextResult:
    if not parent_id or not api_key:
        return TaskContextResult({"tasks": []})
    try:
        async with session.get(
            f"{TODOIST_TASKS_URL}?parent_id={quote(parent_id, safe='')}&limit={limit}",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                raw_tasks: Any
                if isinstance(data, Mapping):
                    raw_tasks = data.get("results", [])
                else:
                    raw_tasks = data
                if isinstance(raw_tasks, list):
                    return TaskContextResult(
                        {
                            "tasks": [
                                _task_summary_from_mapping(task)
                                for task in raw_tasks
                                if isinstance(task, Mapping)
                            ]
                        },
                        status=resp.status,
                    )
                log.warning("child task lookup %s returned non-list JSON", parent_id)
                return TaskContextResult({"tasks": []}, status=resp.status)
            retryable = resp.status in RETRYABLE_TASK_LOOKUP_STATUSES or resp.status >= 500
            log.warning("child task lookup %s returned %d", parent_id, resp.status)
            return TaskContextResult({"tasks": []}, retryable_failure=retryable, status=resp.status)
    except TimeoutError as exc:
        log.warning("child task lookup %s timeout: %s", parent_id, exc)
        return TaskContextResult({"tasks": []}, retryable_failure=True, error=str(exc))
    except Exception as exc:
        log.warning("child task lookup %s error: %s", parent_id, exc)
        return TaskContextResult({"tasks": []}, retryable_failure=True, error=str(exc))


def _context_summary(
    *,
    current_task: Mapping[str, Any],
    parent_task: Mapping[str, Any] | None,
    siblings: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> str:
    current_title = str(current_task.get("content", "this task") or "this task")
    if parent_task:
        parent_title = str(parent_task.get("content", "the parent task") or "the parent task")
        sibling_count = len(siblings)
        return (
            f"This task is a Todoist subtask of '{parent_title}'. "
            f"It should be handled in that parent-task context; {sibling_count} sibling "
            f"task{'s' if sibling_count != 1 else ''} are visible in the same task tree."
        )
    if children:
        return (
            f"'{current_title}' has {len(children)} direct Todoist subtask"
            f"{'s' if len(children) != 1 else ''}. Review child progress before closing or rerouting it."
        )
    return "No native Todoist parent/child task context was found for this event."


async def _build_context_packet(
    session: ClientSession,
    *,
    event_name: str,
    event_data: Mapping[str, Any],
    api_key: str,
) -> dict[str, Any]:
    if event_name == "note:added":
        current_task_id = _first_opaque_id(event_data, "item_id", "itemId")
    else:
        current_task_id = _first_opaque_id(event_data, "id", "task_id")
    parent_id = _first_opaque_id(event_data, "parent_id", "parentId")
    current_task = _task_summary_from_mapping(event_data)
    parent_task: dict[str, Any] | None = None
    siblings: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    source_trace = {
        "payload": True,
        "todoist_parent_lookup": False,
        "todoist_sibling_lookup": False,
        "todoist_child_lookup": False,
    }

    if parent_id and api_key:
        parent_result = await _lookup_task_summary(session, parent_id, api_key)
        if parent_result.data:
            parent_task = parent_result.data
            source_trace["todoist_parent_lookup"] = True
        sibling_result = await _lookup_child_task_summaries(session, parent_id, api_key)
        sibling_tasks = sibling_result.data.get("tasks", [])
        if isinstance(sibling_tasks, list):
            siblings = [
                task for task in sibling_tasks
                if isinstance(task, dict) and str(task.get("id", "")) != current_task_id
            ]
            source_trace["todoist_sibling_lookup"] = True

    if current_task_id and api_key:
        child_result = await _lookup_child_task_summaries(session, current_task_id, api_key)
        child_tasks = child_result.data.get("tasks", [])
        if isinstance(child_tasks, list):
            children = [task for task in child_tasks if isinstance(task, dict)]
            source_trace["todoist_child_lookup"] = True

    return {
        "version": 1,
        "status": "ok",
        "summary": _context_summary(
            current_task=current_task,
            parent_task=parent_task,
            siblings=siblings,
            children=children,
        ),
        "current_task": current_task,
        "task_tree": {
            "parent_task_id": parent_id,
            "parent_task": parent_task,
            "siblings": siblings[:8],
            "direct_children": children[:8],
        },
        "source_trace": source_trace,
    }


async def _enrich_payload_with_context_packet(
    session: ClientSession,
    *,
    payload: dict[str, Any],
    event_name: str,
    event_data: dict[str, Any],
    api_key: str,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    enriched_payload = dict(payload)
    enriched_event_data = dict(event_data)
    try:
        enriched_event_data["context_packet"] = await _build_context_packet(
            session,
            event_name=event_name,
            event_data=enriched_event_data,
            api_key=api_key,
        )
    except Exception as exc:
        log.warning("context packet enrichment failed for %s: %s", _todoist_task_id(event_data), exc)
        enriched_event_data["context_packet"] = {
            "version": 1,
            "status": "failed",
            "summary": "Task-tree context enrichment failed; proceed from the raw Todoist payload.",
            "error_type": type(exc).__name__,
            "current_task": _task_summary_from_mapping(event_data),
            "task_tree": {"parent_task_id": _first_opaque_id(event_data, "parent_id", "parentId")},
            "source_trace": {"payload": True},
        }
    enriched_payload["event_data"] = enriched_event_data
    enriched_body = json.dumps(enriched_payload, separators=(",", ":")).encode("utf-8")
    return enriched_payload, enriched_event_data, enriched_body


def _embedded_task_context(event_data: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("item", "task"):
        value = event_data.get(key)
        if isinstance(value, Mapping):
            context = _task_context_from_mapping(value)
            if context:
                return context
    return {}


async def _lookup_task_context(
    session: ClientSession,
    item_id: str,
    api_key: str,
) -> TaskContextResult:
    try:
        async with session.get(
            f"{TODOIST_TASKS_URL}/{item_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, Mapping):
                    return TaskContextResult(_task_context_from_mapping(data), status=resp.status)
                log.warning("task lookup %s returned non-object JSON", item_id)
                return TaskContextResult({}, status=resp.status)
            retryable = resp.status in RETRYABLE_TASK_LOOKUP_STATUSES or resp.status >= 500
            log.warning("task lookup %s returned %d", item_id, resp.status)
            return TaskContextResult({}, retryable_failure=retryable, status=resp.status)
    except TimeoutError as exc:
        log.warning("task lookup %s timeout: %s", item_id, exc)
        return TaskContextResult({}, retryable_failure=True, error=str(exc))
    except Exception as exc:
        log.warning("task lookup %s error: %s", item_id, exc)
        return TaskContextResult({}, retryable_failure=True, error=str(exc))


async def _resolve_project_id(session: ClientSession, item_id: str, api_key: str) -> str:
    """Look up a task by item_id to get its project_id (needed for note:* events)."""
    return (await _lookup_task_context(session, item_id, api_key)).project_id


_DEFAULT_RESOLVE_PROJECT_ID = _resolve_project_id


async def _resolve_note_parent_context(
    session: ClientSession,
    event_data: Mapping[str, Any],
    api_key: str,
    *,
    require_lookup: bool,
) -> TaskContextResult:
    context = _embedded_task_context(event_data)
    if context:
        return TaskContextResult(context)

    if not require_lookup:
        return TaskContextResult({})

    item_id = _first_opaque_id(event_data, "item_id", "itemId")
    if not item_id or not api_key:
        return TaskContextResult({})
    if _resolve_project_id is not _DEFAULT_RESOLVE_PROJECT_ID:
        project_id = await _resolve_project_id(session, item_id, api_key)
        return TaskContextResult({"project_id": project_id} if project_id else {})
    return await _lookup_task_context(session, item_id, api_key)


def _routing_event_data(
    event_data: Mapping[str, Any],
    project_id: str,
    task_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    routing_data = dict(event_data)
    context = task_context or {}
    if project_id and not _first_opaque_id(routing_data, "project_id"):
        routing_data["project_id"] = project_id
    for field in TASK_CONTEXT_FIELDS:
        value = context.get(field)
        if value and not _first_opaque_id(routing_data, field):
            routing_data[field] = value
    return routing_data


def _has_parent_relevance_context(event_data: Mapping[str, Any]) -> bool:
    return any(_first_opaque_id(event_data, field) for field in TASK_CONTEXT_FIELDS if field != "project_id")


def _load_secret() -> bytes:
    secret = os.environ.get("TODOIST_CLIENT_SECRET", "")
    if not secret:
        log.error("TODOIST_CLIENT_SECRET is not set — refusing to start")
        sys.exit(1)
    return secret.encode()


def _verify(secret: bytes, body: bytes, header_value: str) -> bool:
    expected = base64.b64encode(
        hmac.new(secret, body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, header_value)


def _load_routing() -> tuple[dict[str, object], dict[str, str]]:
    """
    Load routing config. Returns (routes, upstreams) where:
      routes:    project_id → legacy subscription list or conditional route map
      upstreams: subscription_name → upstream base URL
    Falls back to empty dicts on any error.
    """
    try:
        cfg = json.loads(ROUTING_FILE.read_text())
        routes: dict[str, object] = cfg.get("routes", {})
        upstreams: dict[str, str] = cfg.get("upstreams", {})
        return routes, upstreams
    except FileNotFoundError:
        log.warning("routing file not found: %s", ROUTING_FILE)
        return {}, {}
    except Exception as exc:
        log.warning("failed to load routing file: %s", exc)
        return {}, {}


async def _forward(
    session: ClientSession,
    subscription: str,
    upstream: str,
    body: bytes,
    headers: dict[str, str],
    req_id: str,
) -> int:
    url = f"{upstream}/webhooks/{subscription}"
    try:
        async with session.post(
            url,
            data=body,
            headers=headers,
            allow_redirects=False,
        ) as resp:
            log.info("[%s] → %s %d", req_id, url, resp.status)
            return resp.status
    except TimeoutError:
        log.error("[%s] → %s timeout", req_id, url)
        return 504
    except Exception as exc:
        log.error("[%s] → %s error: %s", req_id, url, exc)
        return 502


def _agent_for_subscription(subscription: str) -> str:
    """Map known subscription names to control-ledger agent scopes."""

    return SUBSCRIPTION_AGENT_MAP.get(subscription, "")


def _todoist_task_id(event_data: dict) -> str:
    """Return the stable task/comment parent identifier used in ledger rows."""

    return str(event_data.get("id", "") or event_data.get("item_id", ""))


def _log_ledger_failure(action: str, result: LedgerResult) -> None:
    if not result.success:
        log.warning("ledger %s failed: %s %s", action, result.reason, result.error or "")


def _record_interaction(
    ledger: ControlLedger,
    *,
    interaction_type: str,
    agent: str,
    project_id: str,
    event_data: dict,
    status: str,
    reason: str,
    event_row_id: int | None,
) -> None:
    result = ledger.record_interaction(
        interaction_type=interaction_type,
        agent=agent,
        project_id=project_id,
        todoist_task_id=_todoist_task_id(event_data),
        status=status,
        reason=reason,
        payload=event_data,
        event_row_id=event_row_id,
    )
    _log_ledger_failure("record_interaction", result)


def _record_semantic_interactions(
    ledger: ControlLedger,
    *,
    event_name: str,
    event_data: dict,
    project_id: str,
    event_row_id: int | None,
) -> None:
    for interaction in extract_interactions(event_name, event_data):
        result = ledger.record_interaction(
            interaction_type=interaction.interaction_kind,
            actor=interaction.actor,
            agent=interaction.target.lower(),
            target=interaction.target,
            interaction_kind=interaction.interaction_kind,
            confidence=interaction.confidence,
            project_id=project_id,
            todoist_task_id=interaction.todoist_task_id,
            status="recorded",
            reason=interaction.reason,
            payload=event_data,
            event_row_id=event_row_id,
        )
        _log_ledger_failure("record_semantic_interaction", result)


def _record_inbound_event_or_503(
    ledger: ControlLedger,
    *,
    event_name: str,
    event_data: dict,
    raw_body: bytes,
    headers: Mapping[str, Any],
) -> web.Response | None:
    inbound_result = ledger.record_inbound_event(
        event_name=event_name,
        event_data=event_data,
        raw_body=raw_body,
        headers=headers,
    )
    if inbound_result.success:
        return None
    _log_ledger_failure("record_inbound_event", inbound_result)
    return web.Response(status=503, text="inbound persistence failed")


def _record_event_row(
    ledger: ControlLedger,
    *,
    event_name: str,
    event_data: dict,
) -> int | None:
    event_result = ledger.record_event(
        event_name=event_name,
        event_data=event_data,
        source="proxy",
    )
    _log_ledger_failure("record_event", event_result)
    return event_result.row_id if event_result.success else None


def _retry_at(now: str | None, delay_seconds: int) -> str:
    try:
        base = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    except ValueError:
        base = datetime.now(timezone.utc)
    return (base + timedelta(seconds=delay_seconds)).isoformat()


def _pending_counts() -> dict[str, int]:
    return {"attempted": 0, "succeeded": 0, "retry": 0, "terminal_failed": 0, "skipped": 0}


def _load_pending_payload(
    active_ledger: ControlLedger,
    pending_id: int,
    context,
    counts: dict[str, int],
) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
    try:
        payload = json.loads(context.raw_body)
        event_name = str(payload.get("event_name", context.event_name))
        event_data = payload.get("event_data", {})
        if not isinstance(event_data, dict):
            raise ValueError("event_data is not an object")
        return payload, event_name, event_data
    except (json.JSONDecodeError, AttributeError, ValueError) as exc:
        _log_ledger_failure(
            "update_pending_delivery_state",
            active_ledger.update_pending_delivery_state(
                pending_id,
                state="terminal_failed",
                last_error=f"invalid_payload:{exc}",
            ),
        )
        counts["terminal_failed"] += 1
        return None


async def _process_pending_delivery(
    session: ClientSession,
    active_ledger: ControlLedger,
    pending,
    context,
    *,
    upstreams: Mapping[str, str],
    now: str | None,
    retry_delay_seconds: int,
    counts: dict[str, int],
) -> None:
    loaded = _load_pending_payload(active_ledger, pending.id, context, counts)
    if loaded is None:
        return
    payload, event_name, event_data = loaded
    forward_body = context.raw_body
    enriched_payload, enriched_event_data, enriched_body = await _enrich_payload_with_context_packet(
        session,
        payload=payload,
        event_name=event_name,
        event_data=event_data,
        api_key=os.environ.get("TODOIST_API_KEY", ""),
    )
    if enriched_event_data is not event_data:
        payload = enriched_payload
        event_data = enriched_event_data
        forward_body = enriched_body

    subscription = pending.subscription
    project_id = _first_opaque_id(event_data, "project_id", "projectId") or context.project_id
    delivery_id = context.headers.get("X-Todoist-Delivery-ID", context.delivery_id)
    agent = _agent_for_subscription(subscription)
    if active_ledger.has_successful_delivery(
        source="todoist",
        event_name=event_name,
        event_data=event_data,
        subscription=subscription,
        delivery_id=delivery_id,
        payload=payload,
    ):
        _record_interaction(
            active_ledger,
            interaction_type="forward",
            agent=agent,
            project_id=project_id,
            event_data=event_data,
            status="skipped",
            reason="already_delivered",
            event_row_id=None,
        )
        _log_ledger_failure(
            "update_pending_delivery_state",
            active_ledger.update_pending_delivery_state(pending.id, state="succeeded"),
        )
        counts["skipped"] += 1
        return

    upstream = upstreams.get(subscription, DEFAULT_UPSTREAM)
    forward_headers = dict(context.headers)
    if event_name:
        forward_headers["X-GitHub-Event"] = event_name

    counts["attempted"] += 1
    try:
        status = await _forward(
            session,
            subscription,
            upstream,
            forward_body,
            forward_headers,
            f"drain-{pending.id}",
        )
    except Exception as exc:
        log.error("[drain-%s] forward helper error: %s", pending.id, exc)
        status = 502

    _record_interaction(
        active_ledger,
        interaction_type="forward",
        agent=agent,
        project_id=project_id,
        event_data=event_data,
        status=f"http_{status}",
        reason="forwarded" if status < 300 else "forward_failed",
        event_row_id=None,
    )
    if status < 300:
        _log_ledger_failure(
            "record_successful_delivery",
            active_ledger.record_successful_delivery(
                source="todoist",
                event_name=event_name,
                event_data=event_data,
                subscription=subscription,
                delivery_id=delivery_id,
                payload=payload,
            ),
        )
        _log_ledger_failure(
            "update_pending_delivery_state",
            active_ledger.update_pending_delivery_state(pending.id, state="succeeded"),
        )
        counts["succeeded"] += 1
    elif status < 500:
        _log_ledger_failure(
            "update_pending_delivery_state",
            active_ledger.update_pending_delivery_state(
                pending.id,
                state="terminal_failed",
                last_error=f"http_{status}",
            ),
        )
        counts["terminal_failed"] += 1
    else:
        _log_ledger_failure(
            "update_pending_delivery_state",
            active_ledger.update_pending_delivery_state(
                pending.id,
                state="retry",
                last_error=f"http_{status}",
                next_attempt_at=_retry_at(now, retry_delay_seconds),
                increment_attempt=True,
            ),
        )
        counts["retry"] += 1


def _terminal_route_resolution(
    active_ledger: ControlLedger,
    pending_id: int,
    *,
    project_id: str,
    event_data: dict[str, Any],
    reason: str,
    counts: dict[str, int],
) -> None:
    _record_interaction(
        active_ledger,
        interaction_type="routing",
        agent="",
        project_id=project_id,
        event_data=event_data,
        status="terminal_failed",
        reason=reason,
        event_row_id=None,
    )
    _log_ledger_failure(
        "update_pending_delivery_state",
        active_ledger.update_pending_delivery_state(
            pending_id,
            state="terminal_failed",
            last_error=reason,
        ),
    )
    counts["terminal_failed"] += 1


async def _process_routing_resolution(
    session: ClientSession,
    active_ledger: ControlLedger,
    pending,
    context,
    *,
    routes: Mapping[str, object],
    upstreams: Mapping[str, str],
    now: str | None,
    retry_delay_seconds: int,
    counts: dict[str, int],
) -> None:
    loaded = _load_pending_payload(active_ledger, pending.id, context, counts)
    if loaded is None:
        return
    payload, event_name, event_data = loaded
    if event_name != "note:added":
        _terminal_route_resolution(
            active_ledger,
            pending.id,
            project_id=context.project_id,
            event_data=event_data,
            reason="routing_resolution_non_note_event",
            counts=counts,
        )
        return

    api_key = os.environ.get("TODOIST_API_KEY", "")
    parent_context = await _resolve_note_parent_context(
        session,
        event_data,
        api_key,
        require_lookup=True,
    )
    project_id = parent_context.project_id or context.project_id
    if parent_context.retryable_failure:
        _record_interaction(
            active_ledger,
            interaction_type="routing",
            agent="",
            project_id=project_id,
            event_data=event_data,
            status="lookup_failed",
            reason="task_lookup_retryable",
            event_row_id=None,
        )
        _log_ledger_failure(
            "update_pending_delivery_state",
            active_ledger.update_pending_delivery_state(
                pending.id,
                state="retry",
                last_error="task_lookup_retryable",
                next_attempt_at=_retry_at(now, retry_delay_seconds),
                increment_attempt=True,
            ),
        )
        counts["retry"] += 1
        return

    routing_event_data = _routing_event_data(event_data, project_id, parent_context.data)
    matched_routes = match_routes(routes, event_name, routing_event_data)
    if not project_id or not matched_routes:
        _terminal_route_resolution(
            active_ledger,
            pending.id,
            project_id=project_id,
            event_data=event_data,
            reason="no_route_after_resolution",
            counts=counts,
        )
        return

    delivery_id = context.headers.get("X-Todoist-Delivery-ID", context.delivery_id)
    forward_targets: list[str] = []
    for route in matched_routes:
        subscription = route.subscription
        agent = route.agent or _agent_for_subscription(subscription)
        decision = evaluate_forwarding(
            event_name=event_name,
            project_id=project_id,
            agent=agent,
            source="proxy",
            sentinel_path=DISABLE_FILE,
        )
        _log_ledger_failure(
            "record_routing_decision",
            active_ledger.record_routing_decision(decision=decision, target=subscription),
        )
        if not decision.enabled:
            _record_interaction(
                active_ledger,
                interaction_type="forward",
                agent=agent,
                project_id=project_id,
                event_data=event_data,
                status="suppressed",
                reason=decision.reason,
                event_row_id=None,
            )
            continue
        if active_ledger.has_successful_delivery(
            source="todoist",
            event_name=event_name,
            event_data=event_data,
            subscription=subscription,
            delivery_id=delivery_id,
            payload=payload,
        ):
            _record_interaction(
                active_ledger,
                interaction_type="forward",
                agent=agent,
                project_id=project_id,
                event_data=event_data,
                status="skipped",
                reason="already_delivered",
                event_row_id=None,
            )
            counts["skipped"] += 1
            continue
        forward_targets.append(subscription)

    if forward_targets:
        enqueue_result = active_ledger.enqueue_pending_deliveries_for_inbound(
            inbound_event_id=context.pending.inbound_event_id,
            subscriptions=forward_targets,
            next_attempt_at=now,
        )
        if not enqueue_result.success:
            _log_ledger_failure("enqueue_pending_deliveries_for_inbound", enqueue_result)
            _log_ledger_failure(
                "update_pending_delivery_state",
                active_ledger.update_pending_delivery_state(
                    pending.id,
                    state="retry",
                    last_error="pending_delivery_persistence_failed",
                    next_attempt_at=_retry_at(now, retry_delay_seconds),
                    increment_attempt=True,
                ),
            )
            counts["retry"] += 1
            return

    _log_ledger_failure(
        "update_pending_delivery_state",
        active_ledger.update_pending_delivery_state(pending.id, state="succeeded"),
    )
    for delivery in active_ledger.due_pending_deliveries(now=now):
        if delivery.kind != "delivery" or delivery.inbound_event_id != context.pending.inbound_event_id:
            continue
        delivery_context = active_ledger.pending_delivery_context(delivery.id)
        if delivery_context is None:
            _log_ledger_failure(
                "update_pending_delivery_state",
                active_ledger.update_pending_delivery_state(
                    delivery.id,
                    state="retry",
                    last_error="missing_inbound_context",
                    next_attempt_at=_retry_at(now, retry_delay_seconds),
                    increment_attempt=True,
                ),
            )
            counts["retry"] += 1
            continue
        await _process_pending_delivery(
            session,
            active_ledger,
            delivery,
            delivery_context,
            upstreams=upstreams,
            now=now,
            retry_delay_seconds=retry_delay_seconds,
            counts=counts,
        )


async def drain_pending_deliveries(
    session: ClientSession,
    *,
    ledger: ControlLedger | None = None,
    now: str | None = None,
    limit: int | None = None,
    retry_delay_seconds: int = 60,
) -> dict[str, int]:
    """Drain due local delivery work once; tests call this directly."""

    active_ledger = ledger or ControlLedger()
    _log_ledger_failure("initialize_schema", active_ledger.initialize_schema())
    routes, upstreams = _load_routing()
    counts = _pending_counts()

    for pending in active_ledger.due_pending_deliveries(now=now, limit=limit):
        if pending.kind not in {"delivery", "routing_resolution"}:
            continue
        context = active_ledger.pending_delivery_context(pending.id)
        if context is None:
            _log_ledger_failure(
                "update_pending_delivery_state",
                active_ledger.update_pending_delivery_state(
                    pending.id,
                    state="retry",
                    last_error="missing_inbound_context",
                    next_attempt_at=_retry_at(now, retry_delay_seconds),
                    increment_attempt=True,
                ),
            )
            counts["retry"] += 1
            continue
        if pending.kind == "routing_resolution":
            await _process_routing_resolution(
                session,
                active_ledger,
                pending,
                context,
                routes=routes,
                upstreams=upstreams,
                now=now,
                retry_delay_seconds=retry_delay_seconds,
                counts=counts,
            )
            continue
        if not pending.subscription:
            continue
        await _process_pending_delivery(
            session,
            active_ledger,
            pending,
            context,
            upstreams=upstreams,
            now=now,
            retry_delay_seconds=retry_delay_seconds,
            counts=counts,
        )
    return counts


def _queue_note_routing_resolution_or_503(
    ledger: ControlLedger,
    *,
    event_name: str,
    event_data: dict[str, Any],
    project_id: str,
    raw_body: bytes,
    headers: Mapping[str, Any],
) -> web.Response | None:
    enqueue_result = ledger.record_inbound_event_and_enqueue_pending(
        event_name=event_name,
        event_data=event_data,
        raw_body=raw_body,
        headers=headers,
        kind="routing_resolution",
    )
    if not enqueue_result.success:
        _log_ledger_failure("record_inbound_event_and_enqueue_pending", enqueue_result)
        return web.Response(status=503, text="routing resolution persistence failed")
    event_row_id = _record_event_row(
        ledger,
        event_name=event_name,
        event_data=event_data,
    )
    _record_semantic_interactions(
        ledger,
        event_name=event_name,
        event_data=event_data,
        project_id=project_id,
        event_row_id=event_row_id,
    )
    _record_interaction(
        ledger,
        interaction_type="routing",
        agent="",
        project_id=project_id,
        event_data=event_data,
        status="queued",
        reason="task_lookup_retryable",
        event_row_id=event_row_id,
    )
    return None


async def handle(request: web.Request) -> web.Response:
    req_id = str(uuid.uuid4())[:8]

    if request.method != "POST" or not request.path.startswith("/webhooks/"):
        return web.Response(status=404, text="not found")

    try:
        body = await request.read()
    except Exception:
        log.warning("[%s] failed to read body", req_id)
        return web.Response(status=400, text="bad request")

    if len(body) > MAX_BODY:
        log.warning("[%s] body too large (%d bytes)", req_id, len(body))
        return web.Response(status=413, text="payload too large")

    sig = request.headers.get(TODOIST_SIG_HEADER, "")
    if not sig:
        log.warning("[%s] %s missing", req_id, TODOIST_SIG_HEADER)
        return web.Response(status=401, text="missing signature")

    if not _verify(request.app["secret"], body, sig):
        log.warning("[%s] signature mismatch", req_id)
        return web.Response(status=401, text="invalid signature")

    try:
        payload = json.loads(body)
        event_name = payload.get("event_name", "")
        event_data = payload.get("event_data", {})
        project_id = _first_opaque_id(event_data, "project_id", "projectId")
    except (json.JSONDecodeError, AttributeError):
        log.warning("[%s] unparseable payload", req_id)
        return web.Response(status=400, text="invalid JSON")

    ledger = ControlLedger()
    _log_ledger_failure("initialize_schema", ledger.initialize_schema())
    event_row_id: int | None = None

    if DISABLE_FILE.exists():
        persistence_failure = _record_inbound_event_or_503(
            ledger,
            event_name=event_name,
            event_data=event_data,
            raw_body=body,
            headers=request.headers,
        )
        if persistence_failure is not None:
            return persistence_failure
        event_row_id = _record_event_row(
            ledger,
            event_name=event_name,
            event_data=event_data,
        )
        decision = evaluate_forwarding(
            event_name=event_name,
            project_id=project_id,
            agent="",
            source="proxy",
            sentinel_path=DISABLE_FILE,
        )
        _log_ledger_failure(
            "record_routing_decision",
            ledger.record_routing_decision(
                decision=decision,
                target="",
                event_row_id=event_row_id,
            ),
        )
        _record_interaction(
            ledger,
            interaction_type="forward",
            agent="",
            project_id=project_id,
            event_data=event_data,
            status="suppressed",
            reason=decision.reason,
            event_row_id=event_row_id,
        )
        log.info("[%s] proxy disabled (%s present) — dropping", req_id, DISABLE_FILE)
        return web.Response(status=200, text="proxy disabled")

    if event_name == "item:added":
        due = event_data.get("due")
        if due and due.get("date"):
            is_due, _ = due_status(due, datetime.now(), date.today())
            if not is_due:
                persistence_failure = _record_inbound_event_or_503(
                    ledger,
                    event_name=event_name,
                    event_data=event_data,
                    raw_body=body,
                    headers=request.headers,
                )
                if persistence_failure is not None:
                    return persistence_failure
                event_row_id = _record_event_row(
                    ledger,
                    event_name=event_name,
                    event_data=event_data,
                )
                _record_semantic_interactions(
                    ledger,
                    event_name=event_name,
                    event_data=event_data,
                    project_id=project_id,
                    event_row_id=event_row_id,
                )
                _record_interaction(
                    ledger,
                    interaction_type="routing",
                    agent="",
                    project_id=project_id,
                    event_data=event_data,
                    status="deferred",
                    reason="due_in_future",
                    event_row_id=event_row_id,
                )
                log.info(
                    "[%s] item:added task %s due in the future (%s) — not forwarding, "
                    "due_poller will deliver it when it's actually due",
                    req_id, event_data.get("id", "?"), due.get("date"),
                )
                return web.Response(status=200, text="deferred: due in future")
    routes, _upstreams = _load_routing()
    routing_event_data = _routing_event_data(event_data, project_id)
    matched_routes = []

    if event_name == "note:added":
        api_key = request.app.get("todoist_api_key", "")
        parent_context = await _resolve_note_parent_context(
            request.app["session"],
            event_data,
            api_key,
            require_lookup=not project_id,
        )
        if parent_context.retryable_failure:
            persistence_failure = _queue_note_routing_resolution_or_503(
                ledger,
                event_name=event_name,
                event_data=event_data,
                project_id=project_id,
                raw_body=body,
                headers=request.headers,
            )
            if persistence_failure is not None:
                return persistence_failure
            return web.Response(status=200, text="ok")
        if parent_context.project_id and not project_id:
            project_id = parent_context.project_id
            log.info(
                "[%s] resolved %s item_id %s → project %s",
                req_id,
                event_name,
                event_data.get("item_id", ""),
                project_id,
            )
        routing_event_data = _routing_event_data(event_data, project_id, parent_context.data)
        matched_routes = match_routes(routes, event_name, routing_event_data)

        project_routes = routes.get(project_id) if project_id else None
        needs_parent_lookup = (
            not matched_routes
            and not _has_parent_relevance_context(routing_event_data)
            and (not project_id or isinstance(project_routes, Mapping))
        )
        if needs_parent_lookup:
            parent_context = await _resolve_note_parent_context(
                request.app["session"],
                event_data,
                api_key,
                require_lookup=True,
            )
            if parent_context.retryable_failure:
                persistence_failure = _queue_note_routing_resolution_or_503(
                    ledger,
                    event_name=event_name,
                    event_data=event_data,
                    project_id=project_id,
                    raw_body=body,
                    headers=request.headers,
                )
                if persistence_failure is not None:
                    return persistence_failure
                return web.Response(status=200, text="ok")
            if parent_context.project_id and not project_id:
                project_id = parent_context.project_id
                log.info(
                    "[%s] resolved %s item_id %s → project %s",
                    req_id,
                    event_name,
                    event_data.get("item_id", ""),
                    project_id,
                )
            routing_event_data = _routing_event_data(event_data, project_id, parent_context.data)
            matched_routes = match_routes(routes, event_name, routing_event_data)
    else:
        matched_routes = match_routes(routes, event_name, routing_event_data)

    if not matched_routes:
        persistence_failure = _record_inbound_event_or_503(
            ledger,
            event_name=event_name,
            event_data=event_data,
            raw_body=body,
            headers=request.headers,
        )
        if persistence_failure is not None:
            return persistence_failure
        event_row_id = _record_event_row(
            ledger,
            event_name=event_name,
            event_data=event_data,
        )
        _record_semantic_interactions(
            ledger,
            event_name=event_name,
            event_data=event_data,
            project_id=project_id,
            event_row_id=event_row_id,
        )
        _record_interaction(
            ledger,
            interaction_type="routing",
            agent="",
            project_id=project_id,
            event_data=event_data,
            status="unrouted",
            reason="no_route",
            event_row_id=event_row_id,
        )
        log.info("[%s] no route for project %s — ignored", req_id, project_id or "(missing)")
        return web.Response(status=200, text="no route")

    log.info(
        "[%s] project %s → %s",
        req_id, project_id, ", ".join(route.subscription for route in matched_routes),
    )

    payload, event_data, body = await _enrich_payload_with_context_packet(
        request.app["session"],
        payload=payload,
        event_name=event_name,
        event_data=event_data,
        api_key=request.app.get("todoist_api_key", ""),
    )

    forward_targets: list[str] = []
    routing_decisions = []
    forward_interactions: list[tuple[str, str, str]] = []
    delivery_id = request.headers.get("X-Todoist-Delivery-ID", "")
    for route in matched_routes:
        subscription = route.subscription
        agent = route.agent or _agent_for_subscription(subscription)
        decision = evaluate_forwarding(
            event_name=event_name,
            project_id=project_id,
            agent=agent,
            source="proxy",
            sentinel_path=DISABLE_FILE,
        )
        routing_decisions.append((decision, subscription))
        if decision.enabled:
            if ledger.has_successful_delivery(
                source="todoist",
                event_name=event_name,
                event_data=event_data,
                subscription=subscription,
                delivery_id=delivery_id,
                payload=payload,
            ):
                log.info(
                    "[%s] skipping already-successful delivery for %s (%s)",
                    req_id, subscription, delivery_id or "payload identity",
                )
                forward_interactions.append((agent, "skipped", "already_delivered"))
                continue
            forward_targets.append(subscription)
        else:
            log.info(
                "[%s] forwarding suppressed for %s (%s)",
                req_id, subscription, decision.reason,
            )
            forward_interactions.append((agent, "suppressed", decision.reason))

    if not forward_targets:
        persistence_failure = _record_inbound_event_or_503(
            ledger,
            event_name=event_name,
            event_data=event_data,
            raw_body=body,
            headers=request.headers,
        )
        if persistence_failure is not None:
            return persistence_failure
        event_row_id = _record_event_row(
            ledger,
            event_name=event_name,
            event_data=event_data,
        )
        _record_semantic_interactions(
            ledger,
            event_name=event_name,
            event_data=event_data,
            project_id=project_id,
            event_row_id=event_row_id,
        )
        for decision, subscription in routing_decisions:
            _log_ledger_failure(
                "record_routing_decision",
                ledger.record_routing_decision(
                    decision=decision,
                    target=subscription,
                    event_row_id=event_row_id,
                ),
            )
        for agent, status, reason in forward_interactions:
            _record_interaction(
                ledger,
                interaction_type="forward",
                agent=agent,
                project_id=project_id,
                event_data=event_data,
                status=status,
                reason=reason,
                event_row_id=event_row_id,
            )
        return web.Response(status=200, text="ok")

    enqueue_result = ledger.record_inbound_event_and_enqueue_pending_deliveries(
        event_name=event_name,
        event_data=event_data,
        raw_body=body,
        headers=request.headers,
        subscriptions=forward_targets,
    )
    if not enqueue_result.success:
        _log_ledger_failure("record_inbound_event_and_enqueue_pending_deliveries", enqueue_result)
        return web.Response(status=503, text="pending delivery persistence failed")
    event_row_id = _record_event_row(
        ledger,
        event_name=event_name,
        event_data=event_data,
    )
    _record_semantic_interactions(
        ledger,
        event_name=event_name,
        event_data=event_data,
        project_id=project_id,
        event_row_id=event_row_id,
    )
    for decision, subscription in routing_decisions:
        _log_ledger_failure(
            "record_routing_decision",
            ledger.record_routing_decision(
                decision=decision,
                target=subscription,
                event_row_id=event_row_id,
            ),
        )
    for agent, status, reason in forward_interactions:
        _record_interaction(
            ledger,
            interaction_type="forward",
            agent=agent,
            project_id=project_id,
            event_data=event_data,
            status=status,
            reason=reason,
            event_row_id=event_row_id,
        )
    return web.Response(status=200, text="ok")


async def oauth_callback(request: web.Request) -> web.Response:
    """
    One-time OAuth callback. Todoist redirects here after the user approves
    the app. We exchange the code for an access token, which activates
    webhook delivery for that account.

    Register https://the-data-server.tailf73bbe.ts.net/oauth/callback as the
    redirect URI in the Todoist app console, then visit the authorization URL.
    """
    code = request.rel_url.query.get("code", "")
    error = request.rel_url.query.get("error", "")

    if error:
        log.warning("OAuth error from Todoist: %s", error)
        return web.Response(status=400, text=f"OAuth error: {error}")

    if not code:
        return web.Response(status=400, text="Missing code parameter")

    client_id = os.environ.get("TODOIST_CLIENT_ID", "")
    client_secret = os.environ.get("TODOIST_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return web.Response(
            status=500, text="TODOIST_CLIENT_ID or TODOIST_CLIENT_SECRET not set"
        )

    session: ClientSession = request.app["session"]
    try:
        async with session.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": f"{PUBLIC_BASE}/oauth/callback",
            },
        ) as resp:
            resp_body = await resp.json()
            if resp.status == 200 and "access_token" in resp_body:
                token = resp_body["access_token"]
                log.info("OAuth complete — access_token obtained (%.8s…)", token)
                return web.Response(
                    content_type="text/html",
                    text=(
                        "<h2>✓ Todoist OAuth complete</h2>"
                        "<p>Webhook delivery is now active for this account.</p>"
                        f"<p><code>access_token: {token[:8]}…</code></p>"
                    ),
                )
            log.error("Token exchange failed: %s %s", resp.status, resp_body)
            return web.Response(status=502, text=f"Token exchange failed: {resp_body}")
    except Exception as exc:
        log.error("Token exchange error: %s", exc)
        return web.Response(status=502, text=f"Token exchange error: {exc}")


async def on_startup(app: web.Application) -> None:
    app["session"] = ClientSession()
    app["drain_task"] = asyncio.create_task(_drain_loop(app))


async def on_shutdown(app: web.Application) -> None:
    drain_task = app.get("drain_task")
    if drain_task is not None:
        drain_task.cancel()
        with suppress(asyncio.CancelledError):
            await drain_task
    await app["session"].close()


async def _drain_loop(app: web.Application) -> None:
    while True:
        try:
            await drain_pending_deliveries(app["session"], limit=50)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("pending delivery drain loop failed: %s", exc)
        await asyncio.sleep(DRAIN_INTERVAL_SECONDS)

if __name__ == "__main__":
    secret = _load_secret()
    api_key = os.environ.get("TODOIST_API_KEY", "")
    if not api_key:
        log.warning("TODOIST_API_KEY not set — note:* events cannot be resolved to a project")
    app = web.Application()
    app["secret"] = secret
    app["todoist_api_key"] = api_key
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_route("*", "/{path_info:.*}", handle)
    log.info(
        "todoist-proxy starting on %s:%d (routing: %s)",
        LISTEN_HOST, LISTEN_PORT, ROUTING_FILE,
    )
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)
