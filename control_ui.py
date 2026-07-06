#!/usr/bin/env python3
"""Local stdlib-only HTTP API for the Todoist/Hermes control surface.

The API is intentionally local and narrow: it serves a tiny embedded placeholder
asset set plus JSON endpoints backed only by CONTROL_HOME runtime files. It does
not read or edit Hermes-owned files under ``~/.hermes``.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import html
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from control_ledger import (
    ControlLedger,
    control_config_path,
    ledger_db_path,
    payload_hash,
    resolve_control_home,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_HEADER = "X-Todoist-Control-Token"
TOKEN_ENV = "TODOIST_CONTROL_UI_TOKEN"
TOKEN_FILE_ENV = "TODOIST_CONTROL_UI_TOKEN_FILE"
DEFAULT_TOKEN_FILE_NAME = "control-ui-token.txt"
MAX_LIMIT = 100
DEFAULT_LIMIT = 25
MAX_BODY_BYTES = 64 * 1024
SEMANTIC_TIMELINE_KINDS = ("task_assigned", "comment_mentioned", "due_triggered")
AGENT_COLUMNS = ("Filipp", "Max", "Abra", "Smith", "Hausmeister", "System", "Unknown")
AGENT_KEYS = {name.lower(): name for name in AGENT_COLUMNS}
TIMELINE_WIDTH = 1040
TIMELINE_TOP = 58
TIMELINE_BOTTOM = 68
TIMELINE_LEFT = 164
TIMELINE_RIGHT = 44
TIMELINE_ROW_GAP = 74
TIMELINE_MIN_CHART_HEIGHT = 236

KNOWN_ASSETS = {
    "/": "control-page",
    "/index.html": "control-page",
}

_ROUTING_FILE_FOR_DISPLAY = Path(
    os.environ.get("TODOIST_ROUTING_FILE", Path.home() / ".hermes" / "todoist-routing.json")
)
_SUBSCRIPTIONS_FILE_FOR_DISPLAY = Path(
    os.environ.get("TODOIST_SUBSCRIPTIONS_FILE", Path.home() / ".hermes" / "webhook_subscriptions.json")
)
_PROJECT_NAMES: dict[str, str] = {
    "6ggFh66x4WXVVqGH": "Trigub Technologies Inbox",
    "6gmpjVFv2wVG7XJQ": "LowKeyCodes",
    "6gj88GP4XPg3Qm4r": "Abra",
}
_UID_NAMES: dict[str, str] = {
    "59328091": "Max | CEO",
    "15795569": "Abra | CMO",
    "29584133": "Smith | DevOps",
    "59138424": "Hausmeister",
    "15611160": "Filipp",
}
_SECTION_NAMES: dict[str, str] = {
    "6gpFcCwF29V6QXxx": "CEO",
    "6gpFcCvfqGxWcqwx": "Marketing",
    "6gpFcCxmc39r8MrQ": "Development",
}

_HERMES_ENV_FILE = Path.home() / ".hermes" / ".env"
_LANGFUSE_ENV_LOADED = False

SECRET_KEY_MARKERS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "hmac",
)


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()


def _json_response(status: int, data: dict[str, Any] | list[dict[str, Any]]) -> ApiResponse:
    return ApiResponse(
        status=status,
        body=json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def _load_config(control_home: Path) -> tuple[dict[str, Any], str]:
    path = control_config_path(control_home)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, "missing"
    except (json.JSONDecodeError, OSError):
        return {}, "invalid"
    if not isinstance(data, dict):
        return {}, "invalid"
    return data, "loaded"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SECRET_KEY_MARKERS):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _gate_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
        return bool(value["enabled"])
    return True


def _effective_config(control_home: Path) -> dict[str, Any]:
    config, status = _load_config(control_home)
    global_config = config.get("global", {}) if isinstance(config.get("global", {}), dict) else {}
    projects = config.get("projects", {}) if isinstance(config.get("projects", {}), dict) else {}
    agents = config.get("agents", {}) if isinstance(config.get("agents", {}), dict) else {}
    events = config.get("events", {}) if isinstance(config.get("events", {}), dict) else {}

    return {
        "config_status": status,
        "defaults": {
            "missing_or_invalid_config_forwarding_enabled": True,
            "unspecified_scopes_enabled": True,
        },
        "gates": {
            "global": {
                "forwarding_enabled": global_config.get("forwarding_enabled", True),
                "due_poller_forwarding_enabled": global_config.get("due_poller_forwarding_enabled", True),
            },
            "events": {str(name): _gate_enabled(value) for name, value in events.items()},
            "projects": {
                str(name): {
                    "enabled": _gate_enabled(value),
                    "agents": _redact(value.get("agents", {})) if isinstance(value, dict) else {},
                }
                for name, value in projects.items()
            },
            "agents": {
                str(name): {
                    "enabled": _gate_enabled(value),
                    "events": _redact(value.get("events", {})) if isinstance(value, dict) else {},
                }
                for name, value in agents.items()
            },
        },
        "supported_toggle_scopes": ["global", "event", "project", "project_agent", "agent", "agent_event"],
        "redacted": True,
    }


def _bounded_limit(query: Mapping[str, list[str]]) -> int:
    raw = query.get("limit", [str(DEFAULT_LIMIT)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _connect_existing_ledger(control_home: Path) -> sqlite3.Connection | None:
    db_path = ledger_db_path(control_home)
    if not db_path.exists():
        return None
    return sqlite3.connect(db_path)


def _query_rows(control_home: Path, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    conn = _connect_existing_ledger(control_home)
    if conn is None:
        return []
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _ledger_summary(control_home: Path) -> dict[str, Any]:
    db_path = ledger_db_path(control_home)
    if not db_path.exists():
        return {"available": False, "events": 0, "routing_decisions": 0, "interactions": 0}
    summary = {"available": True, "events": 0, "routing_decisions": 0, "interactions": 0}
    conn = sqlite3.connect(db_path)
    try:
        for table in ("events", "routing_decisions", "interactions"):
            try:
                summary[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                summary[table] = 0
        return summary
    finally:
        conn.close()


def _load_hermes_env_vars() -> None:
    global _LANGFUSE_ENV_LOADED
    if _LANGFUSE_ENV_LOADED:
        return
    _LANGFUSE_ENV_LOADED = True
    try:
        for line in _HERMES_ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("LANGFUSE_") and key not in os.environ:
                os.environ[key] = value.strip()
    except OSError:
        pass


def _fetch_langfuse_traces(limit: int = 50) -> dict[str, Any]:
    _load_hermes_env_vars()
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    pub = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sec = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not (host and pub and sec):
        return {
            "configured": False,
            "error": "Langfuse not configured — set LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY in ~/.hermes/.env",
            "traces": [],
            "total": 0,
        }
    auth = "Basic " + base64.b64encode(f"{pub}:{sec}".encode()).decode()
    params = urlencode({"limit": str(min(limit, 100)), "tags": "platform:webhook"})
    url = f"{host}/api/public/traces?{params}"
    try:
        req = UrlRequest(url, headers={"Authorization": auth, "Accept": "application/json"})
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        traces = data.get("data", []) if isinstance(data, dict) else []
        total = data.get("meta", {}).get("total", len(traces)) if isinstance(data, dict) else len(traces)
        return {"configured": True, "traces": traces, "total": total}
    except HTTPError as exc:
        return {"configured": True, "error": f"Langfuse HTTP {exc.code}: {exc.reason}", "traces": [], "total": 0}
    except URLError as exc:
        return {"configured": True, "error": f"Langfuse unreachable: {exc.reason}", "traces": [], "total": 0}
    except Exception as exc:
        return {"configured": True, "error": str(exc)[:200], "traces": [], "total": 0}


def _status(control_home: Path, host: str, port: int) -> dict[str, Any]:
    config, config_status = _load_config(control_home)
    return {
        "server": {"host": host, "port": port, "cors": "closed"},
        "control": {
            "control_home": str(control_home),
            "config_file": str(control_config_path(control_home)),
            "config_status": config_status,
        },
        "proxy": {
            "forwarding_default_enabled_when_config_missing_or_invalid": True,
        },
        "route_topology": {
            "source": "not_loaded_by_ui_api",
            "configured_in_control_config": isinstance(config.get("routing_file"), str),
        },
        "ledger": _ledger_summary(control_home),
        "redacted": True,
    }


def _events(control_home: Path, limit: int) -> list[dict[str, Any]]:
    return _query_rows(
        control_home,
        """
        SELECT id, event_name, source, project_id, agent, todoist_task_id, payload_hash, received_at
        FROM events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )


def _timeline(control_home: Path, limit: int) -> list[dict[str, Any]]:
    return _query_rows(
        control_home,
        """
        SELECT
            id AS interaction_id,
            created_at AS occurred_at,
            COALESCE(actor, '') AS actor,
            COALESCE(target, agent, '') AS target,
            COALESCE(interaction_kind, interaction_type, '') AS interaction_kind,
            COALESCE(confidence, '') AS confidence,
            event_row_id AS event_id,
            todoist_task_id,
            status,
            reason
        FROM interactions
        WHERE interaction_kind IN (?, ?, ?)
          AND COALESCE(actor, '') != ''
          AND COALESCE(target, '') != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (*SEMANTIC_TIMELINE_KINDS, limit),
    )


MAX_TASK_TREE_ROWS = 5000
MAX_TASK_TREE_DEPTH = 200


TASK_TREE_EDGE_KINDS = ("task_assigned", "comment_mentioned")


def _task_delegation_rows(control_home: Path) -> list[dict[str, Any]]:
    return _query_rows(
        control_home,
        """
        SELECT
            id AS interaction_id,
            todoist_task_id,
            COALESCE(parent_task_id, '') AS parent_task_id,
            actor,
            target,
            interaction_kind,
            COALESCE(confidence, '') AS confidence,
            status,
            reason,
            created_at
        FROM interactions
        WHERE interaction_kind IN (?, ?)
          AND COALESCE(todoist_task_id, '') != ''
          AND COALESCE(actor, '') != ''
          AND COALESCE(target, '') != ''
        ORDER BY id ASC
        LIMIT ?
        """,
        (*TASK_TREE_EDGE_KINDS, MAX_TASK_TREE_ROWS),
    )


def _build_task_tree(rows: list[dict[str, Any]], focus_task_id: str) -> dict[str, Any] | None:
    """Build a delegation tree rooted at the top-most ancestor of ``focus_task_id``.

    A task becomes a node from either signal:
    - ``task_assigned`` (an ``item:added`` with a responsible/assignee), which is
      also the only source of the subtask ``parent_task_id`` link between tasks.
    - ``comment_mentioned`` (an explicit ``@Name`` mention in a comment on that
      task), which lets a task hand off to someone new *without* creating a
      subtask — and lets a task become a tree node even when it never had its
      own ``task_assigned`` row (e.g. assigned via ``item:updated`` instead of
      at creation, which this ledger does not capture).

    Each node shows its full chronological handoff sequence (every matching row
    for that task id, oldest first) rather than a single actor/target pair, so
    "Filipp creates task, Max mentions Smith in a comment on it" renders as one
    task with a two-step handoff instead of requiring a subtask.
    """

    handoffs_by_task: dict[str, list[dict[str, Any]]] = {}
    parent_by_task: dict[str, str] = {}
    for row in rows:
        task_id = str(row.get("todoist_task_id") or "")
        if not task_id:
            continue
        handoffs_by_task.setdefault(task_id, []).append(row)
        parent_id = str(row.get("parent_task_id") or "")
        if parent_id:
            parent_by_task[task_id] = parent_id

    for task_id, task_rows in handoffs_by_task.items():
        task_rows.sort(key=lambda r: str(r.get("created_at") or ""))

    children_by_parent: dict[str, list[str]] = {}
    for task_id, parent_id in parent_by_task.items():
        children_by_parent.setdefault(parent_id, []).append(task_id)

    if focus_task_id not in handoffs_by_task:
        return None

    root_id = focus_task_id
    visited = {root_id}
    for _ in range(MAX_TASK_TREE_DEPTH):
        parent_id = parent_by_task.get(root_id, "")
        if not parent_id or parent_id not in handoffs_by_task or parent_id in visited:
            break
        root_id = parent_id
        visited.add(root_id)

    def build_node(task_id: str, ancestors: frozenset[str]) -> dict[str, Any]:
        handoffs = [
            {
                "actor": row.get("actor") or "",
                "target": row.get("target") or "",
                "kind": row.get("interaction_kind") or "",
                "interaction_id": row.get("interaction_id") or "",
                "confidence": row.get("confidence") or "",
                "status": row.get("status") or "",
                "reason": row.get("reason") or "",
                "created_at": row.get("created_at") or "",
            }
            for row in handoffs_by_task[task_id]
        ]
        children = [
            build_node(child_id, ancestors | {task_id})
            for child_id in children_by_parent.get(task_id, [])
            if child_id not in ancestors
        ]
        return {
            "task_id": task_id,
            "handoffs": handoffs,
            "is_focus": task_id == focus_task_id,
            "children": children,
        }

    return build_node(root_id, frozenset())


def _safe_text(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _uid_display(uid: str) -> str:
    name = _UID_NAMES.get(uid, "")
    if name:
        return f"<strong>{_safe_text(name)}</strong>&thinsp;<code>{_safe_text(uid)}</code>"
    return f"<code>{_safe_text(uid)}</code>"


def _section_display(sid: str) -> str:
    name = _SECTION_NAMES.get(sid, "")
    if name:
        return f"<strong>{_safe_text(name)}</strong>&thinsp;<code>{_safe_text(sid)}</code>"
    return f"<code>{_safe_text(sid)}</code>"


def _agent_column(value: Any) -> str:
    key = str(value or "").strip().lower()
    return AGENT_KEYS.get(key, "Unknown")


def _edge_label(row: Mapping[str, Any]) -> str:
    return str(row.get("interaction_kind") or "").strip()


def _timeline_height(row_count: int) -> int:
    chart_height = max(TIMELINE_MIN_CHART_HEIGHT, max(0, row_count - 1) * TIMELINE_ROW_GAP)
    return TIMELINE_TOP + TIMELINE_BOTTOM + chart_height


def _timeline_y_positions(rows: list[dict[str, Any]], *, top: int) -> dict[int, int]:
    indexed_rows = list(enumerate(rows))
    ordered = sorted(indexed_rows, key=lambda item: str(item[1].get("occurred_at", "")))
    if len(ordered) <= 1:
        return {index: top + (TIMELINE_MIN_CHART_HEIGHT // 2) for index, _ in ordered}
    last_index = len(ordered) - 1
    positions: dict[int, int] = {}
    for age_index, (original_index, _row) in enumerate(ordered):
        positions[original_index] = round(top + (last_index - age_index) * TIMELINE_ROW_GAP)
    return positions


def _format_timeline_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown time"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw.replace("T", " ")[:16]
    return parsed.strftime("%Y-%m-%d %H:%M")


def _timeline_state_class(row: Mapping[str, Any]) -> str:
    status = str(row.get("status", "")).lower()
    reason = str(row.get("reason", "")).lower()
    if "fail" in status or "failed" in reason or status.startswith(("http_4", "http_5")):
        return "failed"
    if status in {"suppressed", "deferred", "unrouted"} or "disabled" in reason or "record" in reason:
        return "disabled"
    return "forwarded"


def _render_timeline_svg(rows: list[dict[str, Any]]) -> str:
    width = TIMELINE_WIDTH
    height = _timeline_height(len(rows))
    top = TIMELINE_TOP
    bottom = TIMELINE_BOTTOM
    left = TIMELINE_LEFT
    right = TIMELINE_RIGHT
    axis_x = 112
    step = (width - left - right) / (len(AGENT_COLUMNS) - 1)
    column_x = {agent: round(left + index * step) for index, agent in enumerate(AGENT_COLUMNS)}
    positions = _timeline_y_positions(rows, top=top)

    column_markup = []
    for agent in AGENT_COLUMNS:
        x = column_x[agent]
        column_markup.append(
            f'<g class="agent-column" data-agent="{agent}">'
            f'<line x1="{x}" y1="{top}" x2="{x}" y2="{height - bottom}" />'
            f'<text x="{x}" y="{height - 18}">{agent}</text>'
            "</g>"
        )

    arrow_markup = []
    for index, row in enumerate(rows):
        actor_label = _safe_text(row.get("actor"))
        target_label = _safe_text(row.get("target"))
        actor = _agent_column(row.get("actor"))
        target = _agent_column(row.get("target"))
        y = positions.get(index, (top + height - bottom) // 2)
        start_x = column_x[actor]
        end_x = column_x[target]
        path_start_x = start_x - 18 if start_x == end_x else start_x
        path_end_x = end_x + 18 if start_x == end_x else end_x
        state = _timeline_state_class(row)
        event_id = _safe_text(row.get("event_id"))
        kind = _safe_text(row.get("interaction_kind"))
        task_id = _safe_text(row.get("todoist_task_id"))
        edge_label = _safe_text(_edge_label(row))
        timestamp = _safe_text(_format_timeline_timestamp(row.get("occurred_at")))
        row_class = "timeline-row has-task-id" if row.get("todoist_task_id") else "timeline-row"
        arrow_markup.append(f'<g class="{row_class}" data-task-id="{task_id}">')
        arrow_markup.append(
            f'<path class="timeline-arrow {state}" data-event-id="{event_id}" '
            f'data-actor="{actor_label}" data-target="{target_label}" '
            f'data-actor-column="{actor}" data-target-column="{target}" '
            f'data-kind="{kind}" data-task-id="{task_id}" data-y="{y}" '
            f'd="M {path_start_x} {y} L {path_end_x} {y}" />'
        )
        arrow_markup.append(
            f'<circle class="timeline-dot {state}" cx="{start_x}" cy="{y}" r="4" />'
            f'<text class="timeline-label timeline-label-route" x="{min(start_x, end_x) + 8}" y="{max(18, y - 14)}">'
            f'{actor_label} → {target_label} · {edge_label}</text>'
            f'<text class="timeline-label timeline-label-task" x="{min(start_x, end_x) + 8}" y="{y + 22}">'
            f'event #{event_id} · task {task_id}</text>'
            f'<line class="time-tick" x1="{axis_x - 7}" y1="{y}" x2="{axis_x + 7}" y2="{y}" />'
            f'<text class="axis-label axis-timestamp" x="10" y="{y + 4}">{timestamp}</text>'
        )
        arrow_markup.append("</g>")

    empty_markup = ""
    if not rows:
        empty_markup = '<text class="empty-state" x="520" y="180">No timeline rows yet</text>'

    return (
        f'<svg id="timeline-svg" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Todoist Hermes interaction timeline" data-testid="timeline-svg">'
        '<defs><marker id="arrow-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>'
        f'<line class="time-axis" x1="{axis_x}" y1="{top}" x2="{axis_x}" y2="{height - bottom}" />'
        f'<text class="axis-label axis-direction" x="10" y="{top - 20}">newest ↑</text>'
        f'<text class="axis-label axis-direction" x="10" y="{height - bottom + 32}">oldest ↓</text>'
        f'{"".join(column_markup)}{"".join(arrow_markup)}{empty_markup}'
        "</svg>"
    )


def _render_toggle_button(scope: str, label: str, enabled: bool, **data: str) -> str:
    data_attrs = " ".join(f'data-{_safe_text(key)}="{_safe_text(value)}"' for key, value in data.items())
    state = "enabled" if enabled else "disabled"
    return (
        f'<button class="toggle is-{state}" type="button" data-scope="{_safe_text(scope)}" {data_attrs} '
        f'data-enabled="{str(enabled).lower()}">'
        f'<span>{_safe_text(label)}</span><b>{state}</b></button>'
    )


def _render_controls(config: dict[str, Any]) -> str:
    gates = config.get("gates", {}) if isinstance(config.get("gates"), dict) else {}
    global_gates = gates.get("global", {}) if isinstance(gates.get("global"), dict) else {}
    event_gates = gates.get("events", {}) if isinstance(gates.get("events"), dict) else {}
    project_gates = gates.get("projects", {}) if isinstance(gates.get("projects"), dict) else {}
    agent_gates = gates.get("agents", {}) if isinstance(gates.get("agents"), dict) else {}

    global_markup = "".join(
        _render_toggle_button(
            "global",
            label.replace("_", " "),
            bool(global_gates.get(label, True)),
            key=label,
        )
        for label in ("forwarding_enabled", "due_poller_forwarding_enabled")
    )
    event_names = sorted({"item:added", "item:updated", "item:completed", "item:uncompleted", "note:added", *event_gates})
    event_markup = "".join(
        _render_toggle_button("event", name, bool(event_gates.get(name, True)), name=name)
        for name in event_names
    )
    project_markup = "".join(
        _render_toggle_button(
            "project",
            name,
            bool(value.get("enabled", True)) if isinstance(value, dict) else bool(value),
            name=name,
        )
        for name, value in sorted(project_gates.items())
    ) or '<p class="hint">No project gates stored yet. Add one with the project id field.</p>'
    agent_markup = "".join(
        _render_toggle_button(
            "agent",
            agent,
            bool(agent_gates.get(agent.lower(), {}).get("enabled", True))
            if isinstance(agent_gates.get(agent.lower()), dict)
            else True,
            name=agent.lower(),
        )
        for agent in AGENT_COLUMNS[:-1]
    )

    return f"""
<div class="control-grid">
  <div class="panel"><h3>Global gates</h3><div class="toggle-grid">{global_markup}</div></div>
  <div class="panel"><h3>Event gates</h3><div class="toggle-grid">{event_markup}</div>
    <form class="inline-form" data-form="event"><input name="name" placeholder="event:name" /><label><input name="enabled" type="checkbox" checked /> enabled</label><button type="submit">set event</button></form>
  </div>
  <div class="panel"><h3>Project gates</h3><div class="toggle-grid" id="project-gates">{project_markup}</div>
    <form class="inline-form" data-form="project"><input name="name" placeholder="project id" /><label><input name="enabled" type="checkbox" checked /> enabled</label><button type="submit">set project</button></form>
  </div>
  <div class="panel"><h3>Agent gates</h3><div class="toggle-grid">{agent_markup}</div></div>
</div>
"""


def _render_event_ledger(events: list[dict[str, Any]], timeline: list[dict[str, Any]]) -> str:
    event_rows = "".join(
        "<tr>"
        f'<td>{_safe_text(row.get("id"))}</td>'
        f'<td>{_safe_text(row.get("event_name"))}</td>'
        f'<td>{_safe_text(row.get("source"))}</td>'
        f'<td>{_safe_text(row.get("project_id"))}</td>'
        f'<td>{_safe_text(row.get("agent"))}</td>'
        f'<td>{_safe_text(row.get("received_at"))}</td>'
        "</tr>"
        for row in events
    ) or '<tr><td colspan="6">No events recorded yet</td></tr>'
    outcome_rows = "".join(
        "<tr>"
        f'<td>{_safe_text(row.get("event_id"))}</td>'
        f'<td>{_safe_text(row.get("actor"))} -> {_safe_text(row.get("target"))}</td>'
        f'<td>{_safe_text(row.get("interaction_kind"))}</td>'
        f'<td>{_safe_text(row.get("todoist_task_id"))}</td>'
        f'<td>{_safe_text(row.get("status"))}</td>'
        f'<td>{_safe_text(row.get("reason"))}</td>'
        "</tr>"
        for row in timeline
    ) or '<tr><td colspan="6">No routing outcomes recorded yet</td></tr>'
    return f"""
<div class="ledger-grid">
  <div class="panel"><h3>Recent events</h3><table id="events-table"><thead><tr><th>id</th><th>event</th><th>source</th><th>project</th><th>agent</th><th>received</th></tr></thead><tbody>{event_rows}</tbody></table></div>
  <div class="panel"><h3>Routing outcomes</h3><table id="outcomes-table"><thead><tr><th>event</th><th>route</th><th>kind</th><th>task</th><th>status</th><th>reason</th></tr></thead><tbody>{outcome_rows}</tbody></table></div>
</div>
"""


def _load_routing_for_display() -> tuple[dict[str, Any], dict[str, str], dict[str, list[str]], str]:
    try:
        cfg = json.loads(_ROUTING_FILE_FOR_DISPLAY.read_text())
    except FileNotFoundError:
        return {}, {}, {}, f"routing file not found: {_ROUTING_FILE_FOR_DISPLAY}"
    except Exception as exc:
        return {}, {}, {}, f"failed to load routing file: {exc}"
    sub_events: dict[str, list[str]] = {}
    try:
        subs = json.loads(_SUBSCRIPTIONS_FILE_FOR_DISPLAY.read_text())
        for name, conf in subs.items():
            if isinstance(conf, dict) and isinstance(conf.get("events"), list):
                sub_events[name] = [str(e) for e in conf["events"]]
    except Exception:
        pass
    return cfg.get("routes", {}), cfg.get("upstreams", {}), sub_events, ""


def _render_routing_rules(routes: dict[str, Any], upstreams: dict[str, str], sub_events: dict[str, list[str]], load_error: str) -> str:
    if load_error:
        return f'<p class="hint">{_safe_text(load_error)}</p>'

    exceptions_html = (
        '<div class="routing-exceptions"><h3>Global exceptions — apply before any project rule</h3>'
        '<ul class="rule-list">'
        '<li><span class="tag tag-exception">⚠ all routes</span> <code>item:added</code> with a'
        ' <strong>future due date</strong> is dropped at the proxy and never forwarded to any subscription'
        ' — <code>due_poller</code> delivers it when the task is actually due.</li>'
        '<li><span class="tag tag-exception">⚠ all routes</span> The <strong>section fallback</strong>'
        ' only fires when the task has <strong>no responsible/assignee at all</strong>. Assigning a task'
        ' to anyone — even someone not matching any rule — suppresses section matching for that event.</li>'
        '<li><span class="tag tag-exception">⚠ note:added only</span> Two-phase routing: if <em>any</em>'
        ' subscription matches via Phase 1 (explicit mention in comment text), Phase 2 (parent-task'
        ' relevance) is skipped for <em>all</em> subscriptions — not just the ones that matched in Phase 1.</li>'
        '</ul></div>'
    )

    project_blocks = []
    for project_id, project_routes in routes.items():
        project_name = _PROJECT_NAMES.get(project_id, "")
        name_html = f"<strong>{_safe_text(project_name)}</strong> " if project_name else ""

        if isinstance(project_routes, list):
            subs = "".join(
                f'<li><strong>{_safe_text(s)}</strong>'
                f' → <code>{_safe_text(upstreams.get(s, "(no upstream)"))}</code>'
                + (
                    " &nbsp; handles: " + " ".join(
                        f'<span class="event-tag">{_safe_text(e)}</span>'
                        for e in sub_events[s]
                    )
                    if s in sub_events else ""
                )
                + "</li>"
                for s in project_routes if isinstance(s, str)
            )
            project_blocks.append(
                f'<div class="routing-project">'
                f'<h3>{name_html}<code>{_safe_text(project_id)}</code>'
                f' <span class="tag tag-broadcast">BROADCAST</span></h3>'
                f'<p class="hint">Flat route — all events forwarded to all subscribers with no filtering'
                f' by assignee, section, or creator.</p>'
                f'<ul class="rule-list">{subs}</ul></div>'
            )
        elif isinstance(project_routes, Mapping):
            sub_blocks = []
            for sub_name, rule in project_routes.items():
                if not isinstance(sub_name, str):
                    continue
                upstream_url = _safe_text(upstreams.get(sub_name, "(no upstream configured)"))
                if not isinstance(rule, Mapping):
                    sub_blocks.append(
                        f'<div class="routing-sub"><h4>{_safe_text(sub_name)}'
                        f' → <code>{upstream_url}</code></h4>'
                        f'<p class="exception-note">⚠ malformed rule (not an object)'
                        f' — fails closed, no delivery</p></div>'
                    )
                    continue

                resp_uids = [str(u) for u in (rule.get("responsible_uids") or []) if u]
                sect_ids = [str(s) for s in (rule.get("section_ids") or []) if s]
                crea_uids = [str(u) for u in (rule.get("creator_uids") or []) if u]
                aliases = [str(a) for a in (rule.get("mention_aliases") or []) if a]

                resp_html = " or ".join(_uid_display(u) for u in resp_uids) if resp_uids else "<em>none</em>"
                sect_html = " or ".join(_section_display(s) for s in sect_ids) if sect_ids else "<em>none</em>"
                crea_html = " or ".join(_uid_display(u) for u in crea_uids) if crea_uids else "<em>none</em>"
                alias_html = (
                    ", ".join(f"<code>{_safe_text(a)}</code>" for a in aliases)
                    if aliases else "<em>none</em>"
                )

                section_li = (
                    f'<li>OR task is <strong>unassigned</strong> AND section = {sect_html}</li>'
                    if sect_ids else ""
                )
                creator_li = f'<li>OR creator/added-by = {crea_html}</li>' if crea_uids else ""

                sub_blocks.append(
                    f'<div class="routing-sub"><h4>{_safe_text(sub_name)} → <code>{upstream_url}</code></h4>'
                    f'<ul class="rule-list">'
                    f'<li><span class="event-tag">item:added &amp; due-poll</span>'
                    f'<ul><li>responsible/assignee = {resp_html}</li>{section_li}'
                    f'<li class="exception-note">⚠ creator is <strong>not</strong> checked'
                    f' for <code>item:added</code></li></ul></li>'
                    f'<li><span class="event-tag">item:updated &nbsp;&nbsp; item:completed'
                    f' &nbsp;&nbsp; item:uncompleted</span>'
                    f'<ul><li>responsible/assignee = {resp_html}</li>{section_li}{creator_li}</ul></li>'
                    f'<li><span class="event-tag">note:added</span> — two-phase'
                    f'<ul><li>Phase 1 (explicit mention; if any subscription matches here,'
                    f' Phase 2 is skipped for all): {alias_html}</li>'
                    f'<li>Phase 2 (parent-task relevance, only runs when Phase 1 matched nothing):'
                    f' same rules as lifecycle</li></ul></li>'
                    f'</ul></div>'
                )
            project_blocks.append(
                f'<div class="routing-project">'
                f'<h3>{name_html}<code>{_safe_text(project_id)}</code>'
                f' <span class="tag tag-conditional">CONDITIONAL</span></h3>'
                f'{"".join(sub_blocks)}</div>'
            )

    content = exceptions_html + "\n".join(project_blocks)
    return content if project_blocks else exceptions_html + '<p class="hint">No routes configured.</p>'


def _control_page(control_home: Path) -> bytes:
    config = _effective_config(control_home)
    events = _events(control_home, DEFAULT_LIMIT)
    timeline = _timeline(control_home, DEFAULT_LIMIT)
    controls = _render_controls(config)
    timeline_svg = _render_timeline_svg(timeline)
    ledger = _render_event_ledger(events, timeline)
    status = _safe_text(config.get("config_status", "unknown"))
    routes, upstreams, sub_events, routing_error = _load_routing_for_display()
    routing_rules = _render_routing_rules(routes, upstreams, sub_events, routing_error)
    routing_file_path = _safe_text(str(_ROUTING_FILE_FOR_DISPLAY))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Todoist Hermes Control</title>
<style>
:root {{
  --ink:#5eead4; --text:#cbd5dc; --muted:#54636f; --line:#1a2530; --line-bright:#2c4048;
  --panel:#0a0e13; --panel-2:#0d131a; --bg:#05070a; --red:#ff5d6c; --amber:#ffb454;
  --glow:rgba(94,234,212,.16); --grid:rgba(94,234,212,.032); --dur:150ms; --rad:2px;
}}
* {{ box-sizing:border-box; }}
html {{ scroll-behavior:smooth; }}
body {{
  margin:0; color:var(--text); font:13px/1.6 ui-monospace,"SF Mono","JetBrains Mono","Geist Mono",Consolas,monospace;
  background:
    linear-gradient(var(--grid) 1px, transparent 1px) 0 0/100% 22px,
    linear-gradient(90deg, var(--grid) 1px, transparent 1px) 0 0/22px 100%,
    var(--bg);
  -webkit-font-smoothing:antialiased;
}}
::selection {{ background:var(--glow); color:var(--ink); }}
::-webkit-scrollbar {{ width:9px; height:9px; }}
::-webkit-scrollbar-track {{ background:var(--panel); }}
::-webkit-scrollbar-thumb {{ background:var(--line-bright); border:2px solid var(--panel); }}
::-webkit-scrollbar-thumb:hover {{ background:var(--ink); }}
main {{ max-width:1180px; margin:0 auto; padding:24px; }}
header {{ position:relative; border:1px solid var(--line); background:var(--panel); padding:16px 18px; margin-bottom:16px; }}
header::before,header::after {{ content:""; position:absolute; width:7px; height:7px; border:1px solid var(--ink); opacity:.65; }}
header::before {{ top:-1px; left:-1px; border-right:0; border-bottom:0; }}
header::after {{ bottom:-1px; right:-1px; border-left:0; border-top:0; }}
h1,h2,h3,p {{ margin:0; }}
h1 {{ display:flex; align-items:center; gap:9px; color:var(--ink); font-size:16px; letter-spacing:.12em; text-transform:uppercase; }}
h1::before {{ content:""; width:7px; height:7px; background:var(--ink); box-shadow:0 0 6px var(--glow); animation:pulse 1.8s ease-in-out infinite; flex:0 0 auto; }}
h2 {{ display:flex; align-items:center; gap:8px; color:var(--ink); font-size:13px; letter-spacing:.06em; text-transform:uppercase; margin-bottom:10px; }}
h2::before {{ content:"//"; color:var(--muted); font-weight:400; }}
h3 {{ font-size:11px; color:var(--amber); margin-bottom:10px; text-transform:uppercase; letter-spacing:.1em; }}
@keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.25; }} }}
@keyframes rise {{ from {{ opacity:0; transform:translateY(4px); }} to {{ opacity:1; transform:translateY(0); }} }}
.status-line {{ color:var(--muted); margin-top:6px; letter-spacing:.02em; }}
.tabs {{ display:grid; grid-template-columns:repeat(5,1fr); gap:8px; margin-bottom:16px; }}
.tabs a {{
  color:var(--muted); text-decoration:none; text-align:center; text-transform:uppercase;
  font-size:11px; letter-spacing:.08em; border:1px solid var(--line); background:var(--panel-2);
  padding:9px 10px; transition:color var(--dur) ease, border-color var(--dur) ease, background var(--dur) ease;
}}
.tabs a:hover {{ color:var(--ink); border-color:var(--line-bright); background:var(--panel); }}
.tabs a:active {{ transform:translateY(1px); }}
section[data-main-section] {{
  border:1px solid var(--line); background:var(--panel); padding:16px; margin-bottom:16px;
  transition:border-color var(--dur) ease; animation:rise 220ms ease both;
}}
section[data-main-section]:hover {{ border-color:var(--line-bright); }}
.timeline-section {{ position:relative; }}
.timeline-section.is-expanded {{ position:fixed; inset:16px; z-index:20; display:flex; flex-direction:column; margin:0; padding:18px; background:var(--bg); border-color:var(--ink); box-shadow:0 0 0 9999px rgba(0,0,0,.78), 0 0 48px var(--glow); }}
.timeline-section.is-expanded .timeline-toolbar {{ flex:0 0 auto; }}
.timeline-section.is-expanded .timeline-frame {{ flex:1 1 auto; max-height:none; }}
.timeline-section.is-expanded #timeline-expand-toggle {{ border-color:var(--ink); background:var(--panel-2); }}
body.timeline-expanded {{ overflow:hidden; }}
.control-grid,.ledger-grid {{ display:grid; gap:10px; }}
.control-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
.ledger-grid {{ grid-template-columns:1fr; }}
.panel {{ border:1px solid var(--line); background:var(--panel-2); padding:13px; overflow:auto; transition:border-color var(--dur) ease; }}
.panel:hover {{ border-color:var(--line-bright); }}
input {{ width:100%; color:var(--text); background:var(--bg); border:1px solid var(--line); border-radius:var(--rad); padding:8px; font:inherit; transition:border-color var(--dur) ease; }}
input:focus {{ outline:none; border-color:var(--ink); box-shadow:0 0 0 1px var(--ink) inset; }}
button {{
  color:var(--ink); background:var(--panel); border:1px solid var(--line); border-radius:var(--rad);
  padding:8px 10px; font:inherit; letter-spacing:.02em; cursor:pointer;
  transition:border-color var(--dur) ease, box-shadow var(--dur) ease, transform 100ms ease;
}}
button:hover {{ border-color:var(--ink); box-shadow:0 0 0 1px var(--ink) inset; }}
button:active {{ transform:scale(.97); }}
.timeline-toolbar {{ display:flex; justify-content:space-between; gap:10px; align-items:center; margin-bottom:10px; flex-wrap:wrap; }}
.timeline-toolbar .hint {{ max-width:68ch; }}
.timeline-toolbar-actions {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
.tree-search-form {{ display:flex; gap:6px; }}
.tree-search-form input {{ width:120px; }}
.is-hidden {{ display:none !important; }}
.timeline-row {{ cursor:default; }}
.timeline-row.has-task-id {{ cursor:pointer; }}
.timeline-row.has-task-id:hover .timeline-arrow {{ stroke:var(--amber); }}
.timeline-row.has-task-id:hover .timeline-dot {{ fill:var(--amber); }}
.timeline-row.has-task-id:hover .timeline-label-task {{ fill:var(--amber); }}
.timeline-frame {{ overflow:auto; max-height:460px; border:1px solid var(--line); background:var(--bg); }}
.timeline-frame svg {{ display:block; border:0; min-width:1040px; transition:min-width var(--dur) ease; }}
.timeline-section.is-expanded svg {{ min-width:1480px; }}
.timeline-section.is-expanded .timeline-label {{ font-size:12px; }}
.tree-view {{ display:grid; grid-template-columns:minmax(680px,1fr) 300px; gap:12px; padding:12px; font-size:12px; min-width:1020px; }}
.tree-legend {{ color:var(--muted); margin:0 0 10px; grid-column:1 / -1; }}
.tree-canvas {{ overflow:auto; border:1px solid var(--line); background:radial-gradient(circle at 50% 0, rgba(94,234,212,.055), transparent 34%), var(--bg); }}
.tree-graph {{ display:block; min-width:760px; border:0; background:transparent; }}
.tree-trunk,.tree-link {{ fill:none; stroke:var(--line-bright); stroke-width:1.5; }}
.tree-link {{ marker-end:url(#tree-arrow-head); }}
.tree-fork {{ stroke:var(--ink); stroke-width:1.5; stroke-dasharray:3 5; }}
#tree-arrow-head path {{ fill:var(--line-bright); }}
.tree-node {{ cursor:pointer; }}
.tree-node-hit {{ fill:transparent; }}
.tree-node-dot {{ fill:var(--panel); stroke:var(--line-bright); stroke-width:2; transition:stroke var(--dur) ease, fill var(--dur) ease; }}
.tree-node.is-focus .tree-node-dot {{ stroke:var(--ink); fill:rgba(94,234,212,.08); }}
.tree-node.is-selected .tree-node-dot {{ stroke:var(--amber); fill:rgba(255,180,84,.09); }}
.tree-node:hover .tree-node-dot {{ stroke:var(--amber); }}
.tree-node-code {{ fill:var(--amber); font-size:10px; text-anchor:middle; font-family:'SF Mono','JetBrains Mono',monospace; }}
.tree-node-label {{ fill:var(--text); font-size:11px; text-anchor:middle; }}
.tree-node-subtitle {{ fill:var(--muted); font-size:10px; text-anchor:middle; }}
.tree-branch-label {{ fill:var(--muted); font-size:10px; text-anchor:middle; }}
.tree-inspector {{ border:1px solid var(--line); background:var(--panel); padding:12px; min-height:260px; align-self:start; position:sticky; top:12px; }}
.tree-inspector h3 {{ margin:0 0 8px; font-size:12px; color:var(--amber); text-transform:uppercase; letter-spacing:.06em; }}
.tree-inspector-task {{ color:var(--muted); font:11px 'SF Mono','JetBrains Mono',monospace; overflow-wrap:anywhere; margin-bottom:12px; }}
.tree-inspector-row {{ border-top:1px dashed rgba(180,255,211,.16); padding:8px 0; }}
.tree-inspector-row:first-of-type {{ border-top:0; }}
.tree-edge {{ color:var(--text); }}
.tree-kind {{ color:var(--amber); font-size:10px; text-transform:uppercase; letter-spacing:.05em; }}
.tree-task-id {{ color:var(--muted); font-size:11px; cursor:pointer; }}
.tree-task-id:hover {{ color:var(--ink); }}
.tree-meta {{ color:var(--muted); font-size:10px; margin-top:3px; overflow-wrap:anywhere; }}
.tree-empty-handoff {{ color:var(--muted); font-style:italic; }}
.tree-empty {{ color:var(--muted); padding:12px; }}
.toggle-grid {{ display:grid; gap:6px; }}
.toggle {{
  display:flex; justify-content:space-between; gap:10px; text-align:left; cursor:pointer;
  border:1px solid var(--line); background:var(--panel); padding:8px 10px; border-radius:var(--rad);
  transition:border-color var(--dur) ease, background var(--dur) ease, transform 100ms ease;
}}
.toggle:hover {{ border-color:var(--line-bright); }}
.toggle:active {{ transform:scale(.985); }}
.toggle b {{ color:var(--muted); font-weight:400; text-transform:uppercase; font-size:10px; letter-spacing:.06em; }}
.toggle.is-enabled b {{ color:var(--ink); }}
.toggle.is-enabled b::before {{ content:"● "; }}
.toggle.is-disabled b {{ color:var(--red); }}
.toggle.is-disabled b::before {{ content:"○ "; }}
.inline-form {{ display:grid; grid-template-columns:1fr auto auto; gap:8px; align-items:center; margin-top:10px; }}
.hint,.empty-state,.axis-label {{ fill:var(--muted); color:var(--muted); }}
svg {{ width:100%; border:1px solid var(--line); background:var(--bg); }}
.agent-column line,.time-axis {{ stroke:var(--line); stroke-width:1; }}
.time-tick {{ stroke:var(--muted); stroke-width:1; }}
.agent-column text,.timeline-label {{ fill:var(--muted); font-size:11px; text-anchor:middle; }}
.agent-column text {{ fill:var(--amber); }}
.timeline-label,.axis-label {{ text-anchor:start; }}
.timeline-label-route {{ fill:var(--text); }}
.timeline-label-task {{ fill:var(--muted); font-size:10px; }}
.axis-timestamp {{ font-size:10px; }}
.axis-direction {{ font-size:10px; fill:var(--amber); }}
#arrow-head path {{ fill:var(--ink); }}
.timeline-arrow {{ fill:none; stroke:var(--ink); stroke-width:2; marker-end:url(#arrow-head); }}
.timeline-arrow.disabled {{ stroke:var(--muted); stroke-dasharray:5 5; }}
.timeline-arrow.failed {{ stroke:var(--red); }}
.timeline-dot {{ fill:var(--ink); }}
.timeline-dot.disabled {{ fill:var(--muted); }}
.timeline-dot.failed {{ fill:var(--red); }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th,td {{ border-bottom:1px solid var(--line); padding:7px 6px; text-align:left; vertical-align:top; }}
th {{ color:var(--amber); font-weight:400; text-transform:uppercase; font-size:10px; letter-spacing:.06em; }}
tbody tr {{ transition:background var(--dur) ease; }}
tbody tr:hover {{ background:var(--panel); }}
td:first-child,th:first-child {{ font-variant-numeric:tabular-nums; color:var(--muted); }}
@media (max-width:760px) {{ .control-grid,.tabs {{ grid-template-columns:1fr; }} }}
.routing-project {{ margin-top:20px; }}
.routing-sub {{ margin:10px 0 10px 14px; border-left:2px solid var(--line); padding-left:14px; }}
.routing-exceptions {{ margin-bottom:20px; padding:12px; border:1px solid rgba(255,93,108,.35); background:rgba(255,93,108,.04); }}
.rule-list {{ margin:6px 0; padding-left:18px; line-height:1.85; }}
.rule-list ul {{ margin-top:4px; }}
.routing-sub h4 {{ font-size:12px; color:var(--text); margin:8px 0 4px; font-weight:600; }}
.tag {{ font-size:10px; padding:2px 6px; border:1px solid; border-radius:var(--rad); font-weight:400; text-transform:uppercase; letter-spacing:.04em; vertical-align:middle; }}
.tag-broadcast {{ color:var(--amber); border-color:var(--amber); }}
.tag-conditional {{ color:var(--ink); border-color:var(--ink); }}
.tag-exception {{ color:var(--red); border-color:var(--red); }}
.event-tag {{ color:var(--amber); font-size:11px; font-weight:600; }}
.exception-note {{ color:var(--red); }}
</style>
</head>
<body>
<main>
<header><h1>Todoist Hermes Control</h1><p class="status-line">local-only control surface / config: {status} / token stays outside API responses</p></header>
<nav class="tabs" aria-label="Main sections"><a href="#controls">Controls</a><a href="#timeline">Timeline</a><a href="#event-ledger">Event ledger</a><a href="#session-insights">Session insights</a><a href="#routing-rules">Routing rules</a></nav>
<section id="controls" data-main-section="Controls"><h2>Controls</h2>{controls}</section>
<section id="timeline" class="timeline-section" data-main-section="Timeline" data-expanded="false"><div class="timeline-toolbar"><div><h2>Timeline</h2><p class="hint" id="timeline-hint">Semantic graph: timestamped, vertically scrollable, and fullscreen when expanded. Click any arrow or task id to open its delegation tree.</p></div><div class="timeline-toolbar-actions"><form id="tree-search-form" class="tree-search-form"><input id="tree-search-input" placeholder="task id" autocomplete="off" /><button type="submit">View tree</button></form><button id="tree-back-button" type="button" class="is-hidden">&larr; Back to timeline</button><button id="timeline-expand-toggle" type="button" aria-expanded="false" aria-controls="timeline-frame">Expand timeline</button></div></div><div id="timeline-frame" class="timeline-frame">{timeline_svg}</div></section>
<section id="event-ledger" data-main-section="Event ledger"><h2>Event ledger</h2><div id="ledger-frame">{ledger}</div></section>
<section id="session-insights" data-main-section="Session insights"><h2>Session insights</h2><p class="hint">Langfuse traces for webhook-triggered Hermes sessions. Matched by <code>platform:webhook</code> tag. Refreshes every 30 s.</p><div id="lf-status"></div><div id="lf-agg" style="margin-bottom:14px"><h3>Per-profile aggregates</h3><table><thead><tr><th>profile</th><th>sessions</th><th>total cost ($)</th><th>avg cost ($)</th><th>avg api calls</th><th>avg latency</th></tr></thead><tbody id="lf-agg-body"><tr><td colspan="6" class="hint">Loading…</td></tr></tbody></table></div><div id="lf-traces"><h3>Recent sessions <span id="lf-count" style="color:var(--muted);font-weight:400"></span></h3><table><thead><tr><th>time</th><th>profile</th><th>cost ($)</th><th>api calls</th><th>latency</th></tr></thead><tbody id="lf-trace-body"><tr><td colspan="5" class="hint">Loading…</td></tr></tbody></table></div></section>
<section id="routing-rules" data-main-section="Routing rules"><h2>Routing rules</h2><p class="hint">Loaded from <code>{routing_file_path}</code> · hot-reloaded per request · no proxy restart needed</p>{routing_rules}</section>
</main>
<script>
const AGENT_COLUMNS = {json.dumps(AGENT_COLUMNS)};
const CONTROL_TOKEN_HEADER = {json.dumps(TOKEN_HEADER)};
const CONTROL_TOKEN_STORAGE_KEY = "todoist-control-ui-token";
const TIMELINE = {{width:1040, top:58, bottom:68, left:164, right:44, rowGap:74, minChartHeight:236, axisX:112}};
const esc = value => String(value ?? "").replace(/[&<>\"]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c]));
const agentColumn = value => AGENT_COLUMNS.find(name => name.toLowerCase() === String(value || "").toLowerCase()) || "Unknown";
function timelineHeight(rowCount) {{ return TIMELINE.top + TIMELINE.bottom + Math.max(TIMELINE.minChartHeight, Math.max(0, rowCount - 1) * TIMELINE.rowGap); }}
function formatTimestamp(value) {{
  const raw = String(value || "").trim();
  if (!raw) return "unknown time";
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return raw.replace("T", " ").slice(0, 16);
  const pad = number => String(number).padStart(2, "0");
  return `${{parsed.getFullYear()}}-${{pad(parsed.getMonth() + 1)}}-${{pad(parsed.getDate())}} ${{pad(parsed.getHours())}}:${{pad(parsed.getMinutes())}}`;
}}
async function getJson(url) {{ const res = await fetch(url, {{cache:"no-store"}}); return res.json(); }}
function getControlToken() {{ return sessionStorage.getItem(CONTROL_TOKEN_STORAGE_KEY) || ""; }}
function promptForControlToken() {{
  const token = window.prompt(`Control token required. Paste the value for ${{CONTROL_TOKEN_HEADER}}:`) || "";
  const trimmed = token.trim();
  if (trimmed) sessionStorage.setItem(CONTROL_TOKEN_STORAGE_KEY, trimmed);
  return trimmed;
}}
function controlTokenHeaders() {{
  const token = getControlToken() || promptForControlToken();
  if (!token) return null;
  return {{"Content-Type":"application/json", [CONTROL_TOKEN_HEADER]: token}};
}}
async function postToggle(payload) {{
  const headers = controlTokenHeaders();
  if (!headers) return;
  let res = await fetch("/api/config/toggle", {{method:"POST", headers, body:JSON.stringify(payload)}});
  if (res.status === 403) {{
    sessionStorage.removeItem(CONTROL_TOKEN_STORAGE_KEY);
    const retryHeaders = controlTokenHeaders();
    if (!retryHeaders) return;
    res = await fetch("/api/config/toggle", {{method:"POST", headers:retryHeaders, body:JSON.stringify(payload)}});
  }}
  if (res.ok) window.location.reload();
  else window.alert("Control update failed: " + res.status);
}}
function renderLedger(events, timeline) {{
  const eventRows = (events || []).map(row => `<tr><td>${{esc(row.id)}}</td><td>${{esc(row.event_name)}}</td><td>${{esc(row.source)}}</td><td>${{esc(row.project_id)}}</td><td>${{esc(row.agent)}}</td><td>${{esc(row.received_at)}}</td></tr>`).join("") || '<tr><td colspan="6">No events recorded yet</td></tr>';
  const outcomeRows = (timeline || []).map(row => `<tr><td>${{esc(row.event_id)}}</td><td>${{esc(row.actor)}} -> ${{esc(row.target)}}</td><td>${{esc(row.interaction_kind)}}</td><td>${{esc(row.todoist_task_id)}}</td><td>${{esc(row.status)}}</td><td>${{esc(row.reason)}}</td></tr>`).join("") || '<tr><td colspan="6">No routing outcomes recorded yet</td></tr>';
  document.querySelector("#events-table tbody").innerHTML = eventRows;
  document.querySelector("#outcomes-table tbody").innerHTML = outcomeRows;
}}
function renderSvg(rows) {{
  const width = TIMELINE.width, height = timelineHeight(rows.length), top = TIMELINE.top, bottom = TIMELINE.bottom, left = TIMELINE.left, right = TIMELINE.right;
  const step = (width - left - right) / (AGENT_COLUMNS.length - 1);
  const x = Object.fromEntries(AGENT_COLUMNS.map((name, i) => [name, Math.round(left + i * step)]));
  const ordered = rows.map((row, index) => [row, index]).sort((a,b) => String(a[0].occurred_at || "").localeCompare(String(b[0].occurred_at || "")));
  const yByIndex = {{}};
  ordered.forEach((pair, ageIndex) => {{ yByIndex[pair[1]] = ordered.length <= 1 ? top + Math.round(TIMELINE.minChartHeight / 2) : Math.round(top + ((ordered.length - 1 - ageIndex) * TIMELINE.rowGap)); }});
  const columns = AGENT_COLUMNS.map(name => `<g class="agent-column" data-agent="${{name}}"><line x1="${{x[name]}}" y1="${{top}}" x2="${{x[name]}}" y2="${{height-bottom}}"/><text x="${{x[name]}}" y="${{height-18}}">${{name}}</text></g>`).join("");
  const arrows = rows.map((row, index) => {{
    const actor = agentColumn(row.actor), target = agentColumn(row.target), y = yByIndex[index], start = x[actor], end = x[target];
    const edgeLabel = `${{esc(row.actor)}} → ${{esc(row.target)}} · ${{esc(row.interaction_kind)}}`;
    const status = String(row.status || "").toLowerCase(), reason = String(row.reason || "").toLowerCase();
    const state = status.includes("fail") || reason.includes("failed") || status.startsWith("http_4") || status.startsWith("http_5") ? "failed" : (status === "suppressed" || status === "deferred" || status === "unrouted" || reason.includes("disabled") || reason.includes("record") ? "disabled" : "forwarded");
    const pathStart = start === end ? start - 18 : start;
    const pathEnd = start === end ? end + 18 : end;
    const textX = Math.min(start, end) + 8;
    const taskId = esc(row.todoist_task_id);
    const rowClass = row.todoist_task_id ? "timeline-row has-task-id" : "timeline-row";
    return `<g class="${{rowClass}}" data-task-id="${{taskId}}" data-interaction-id="${{esc(row.interaction_id)}}"><path class="timeline-arrow ${{state}}" data-event-id="${{esc(row.event_id)}}" data-interaction-id="${{esc(row.interaction_id)}}" data-actor="${{esc(row.actor)}}" data-target="${{esc(row.target)}}" data-actor-column="${{actor}}" data-target-column="${{target}}" data-kind="${{esc(row.interaction_kind)}}" data-task-id="${{taskId}}" data-y="${{y}}" d="M ${{pathStart}} ${{y}} L ${{pathEnd}} ${{y}}"/><circle class="timeline-dot ${{state}}" cx="${{start}}" cy="${{y}}" r="4"/><text class="timeline-label timeline-label-route" x="${{textX}}" y="${{Math.max(18, y - 14)}}">${{edgeLabel}}</text><text class="timeline-label timeline-label-task" x="${{textX}}" y="${{y + 22}}">event #${{esc(row.event_id)}} · task ${{esc(row.todoist_task_id)}}</text><line class="time-tick" x1="${{TIMELINE.axisX - 7}}" y1="${{y}}" x2="${{TIMELINE.axisX + 7}}" y2="${{y}}"/><text class="axis-label axis-timestamp" x="10" y="${{y + 4}}">${{esc(formatTimestamp(row.occurred_at))}}</text></g>`;
  }}).join("");
  return `<svg id="timeline-svg" viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Todoist Hermes interaction timeline" data-testid="timeline-svg"><defs><marker id="arrow-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs><line class="time-axis" x1="${{TIMELINE.axisX}}" y1="${{top}}" x2="${{TIMELINE.axisX}}" y2="${{height-bottom}}"/><text class="axis-label axis-direction" x="10" y="${{top-20}}">newest ↑</text><text class="axis-label axis-direction" x="10" y="${{height-bottom+32}}">oldest ↓</text>${{columns}}${{arrows || '<text class="empty-state" x="520" y="180">No timeline rows yet</text>'}}</svg>`;
}}
function bindTimelineExpand() {{
  const button = document.querySelector("#timeline-expand-toggle");
  const frame = document.querySelector("#timeline-frame");
  const section = document.querySelector("#timeline");
  if (!button || !frame || !section) return;
  button.onclick = () => {{
    const expanded = !section.classList.contains("is-expanded");
    section.classList.toggle("is-expanded", expanded);
    section.dataset.expanded = String(expanded);
    document.body.classList.toggle("timeline-expanded", expanded);
    button.setAttribute("aria-expanded", String(expanded));
    button.textContent = expanded ? "Collapse timeline" : "Expand timeline";
  }};
}}
function bindToggles() {{
  document.querySelectorAll(".toggle").forEach(button => button.onclick = () => {{
    const enabled = button.dataset.enabled !== "true";
    const scope = button.dataset.scope;
    const payload = {{scope, enabled}};
    if (scope === "global") payload.key = button.dataset.key;
    if (scope === "event" || scope === "project" || scope === "agent") payload.name = button.dataset.name;
    postToggle(payload);
  }});
  document.querySelectorAll("form[data-form]").forEach(form => form.onsubmit = event => {{
    event.preventDefault();
    const data = new FormData(form);
    postToggle({{scope:form.dataset.form, name:String(data.get("name") || "").trim(), enabled:data.get("enabled") === "on"}});
  }});
}}
let timelineViewMode = "timeline";
const TREE_KIND_LABEL = {{task_assigned: "assigned", comment_mentioned: "mentioned"}};
const TREE = {{nodeRadius:28, marginX:58, marginY:50, siblingGap:96, levelGap:126, leafWidth:148}};
let currentTreeIndex = {{}};
function eventId(handoff, taskId, index) {{ return handoff.interaction_id ? `interaction:${{handoff.interaction_id}}` : `${{taskId}}::${{index}}`; }}
function compactTaskId(taskId) {{ const value = String(taskId || ""); return value.length > 14 ? `${{value.slice(0, 6)}}…${{value.slice(-5)}}` : value; }}
function eventDecisionLabel(event) {{
  const handoff = event.handoff;
  if (!handoff) return "unclaimed";
  const target = String(handoff.target || "unknown").replace(/\\s*\\|.*$/, "");
  return target || "unknown";
}}
function eventSubtitle(event) {{
  const childCount = (event.children || []).length;
  if (childCount > 1) return `${{childCount}} parallel branches`;
  if (childCount === 1) return "continues";
  return TREE_KIND_LABEL[event.handoff?.kind] || event.handoff?.kind || "event";
}}
function eventSequence(taskNode, focusInteractionId) {{
  const handoffs = taskNode.handoffs || [];
  const source = handoffs.length ? handoffs : [{{actor:"", target:"", kind:"task", confidence:"", status:"", reason:"", created_at:""}}];
  return source.map((handoff, index) => ({{
    id: eventId(handoff, taskNode.task_id, index),
    taskId: taskNode.task_id,
    eventIndex: index + 1,
    handoff,
    isFocusEvent: focusInteractionId ? String(handoff.interaction_id || "") === String(focusInteractionId) : Boolean(taskNode.is_focus && index === 0),
    children: [],
  }}));
}}
function buildEventGraph(taskNode, focusInteractionId) {{
  const events = eventSequence(taskNode, focusInteractionId);
  for (let index = 0; index < events.length - 1; index += 1) events[index].children.push(events[index + 1]);
  const tail = events[events.length - 1];
  for (const childTask of (taskNode.children || [])) tail.children.push(buildEventGraph(childTask, focusInteractionId));
  return events[0];
}}
function measureTree(event, depth = 0, levels = []) {{
  const children = (event.children || []).map(child => measureTree(child, depth + 1, levels));
  const childWidth = children.length ? children.reduce((sum, child) => sum + child.width, 0) + (children.length - 1) * TREE.siblingGap : 0;
  levels[depth] = true;
  return {{event, children, depth, width: Math.max(TREE.leafWidth, childWidth)}};
}}
function treeLevelOffsets(levels) {{
  return levels.map((_, depth) => TREE.marginY + depth * TREE.levelGap);
}}
function placeTree(measured, left, offsets, nodes, links) {{
  const x = Math.round(left + measured.width / 2);
  const y = offsets[measured.depth];
  const point = {{x, y}};
  nodes.push({{measured, x, y}});
  const childWidth = measured.children.length ? measured.children.reduce((sum, child) => sum + child.width, 0) + (measured.children.length - 1) * TREE.siblingGap : 0;
  let childLeft = left + Math.max(0, (measured.width - childWidth) / 2);
  measured.children.forEach(child => {{
    const childPoint = placeTree(child, childLeft, offsets, nodes, links);
    links.push({{from: point, to: childPoint, parallel: measured.children.length > 1}});
    childLeft += child.width + TREE.siblingGap;
  }});
  return point;
}}
function renderInspectorHandoff(handoff) {{
  const edge = `${{esc(handoff.actor)}} <span class="tree-arrow">&rarr;</span> ${{esc(handoff.target)}}`;
  const kind = esc(TREE_KIND_LABEL[handoff.kind] || handoff.kind || "");
  const when = handoff.created_at ? esc(formatTimestamp(handoff.created_at)) : "";
  const reason = handoff.reason ? `<div class="tree-meta">${{esc(handoff.reason)}}</div>` : "";
  return `<div class="tree-inspector-row"><div><span class="tree-edge">${{edge}}</span> <span class="tree-kind">${{kind}}</span></div><div class="tree-meta">${{when}}</div>${{reason}}</div>`;
}}
function flattenTreeEntries(measured, entries = []) {{
  entries.push(measured.event);
  measured.children.forEach(child => flattenTreeEntries(child, entries));
  return entries;
}}
function indexEventNodes(event, index = {{}}) {{
  index[event.id] = event;
  (event.children || []).forEach(child => indexEventNodes(child, index));
  return index;
}}
function focusedTreeNode(measured) {{ return flattenTreeEntries(measured).find(event => event.isFocusEvent) || measured.event; }}
function renderTreeInspectorNode(event) {{
  const rows = event.handoff ? renderInspectorHandoff(event.handoff) : '<div class="tree-empty-handoff">No handoff recorded for this event.</div>';
  return `<h3>${{esc(event.isFocusEvent ? "Focused event" : "Selected event")}}</h3><div class="tree-inspector-task">task ${{esc(event.taskId)}} · event ${{esc(event.eventIndex)}}</div><div class="tree-meta">${{esc(eventSubtitle(event))}}</div>${{rows}}`;
}}
function renderTreeInspector(event) {{
  return `<aside class="tree-inspector" id="tree-inspector">${{renderTreeInspectorNode(event)}}</aside>`;
}}
function inspectTreeNode(eventNodeId) {{
  const node = currentTreeIndex[String(eventNodeId || "")];
  const inspector = document.querySelector("#tree-inspector");
  if (!node || !inspector) return;
  inspector.innerHTML = renderTreeInspectorNode(node);
  document.querySelectorAll(".tree-node.is-selected").forEach(el => el.classList.remove("is-selected"));
  const selected = Array.from(document.querySelectorAll("[data-inspect-event]")).find(el => el.getAttribute("data-inspect-event") === String(eventNodeId));
  if (selected) selected.classList.add("is-selected");
}}
function renderTreeNode(entry) {{
  const event = entry.measured.event;
  const cls = event.isFocusEvent ? "tree-node is-focus" : "tree-node";
  return `<g class="${{cls}}" data-inspect-event="${{esc(event.id)}}" tabindex="0" role="button" aria-label="Inspect event ${{esc(event.eventIndex)}} on task ${{esc(event.taskId)}}"><circle class="tree-node-hit" cx="${{entry.x}}" cy="${{entry.y}}" r="44"/><circle class="tree-node-dot" cx="${{entry.x}}" cy="${{entry.y}}" r="${{TREE.nodeRadius}}"/><text class="tree-node-code" x="${{entry.x}}" y="${{entry.y - 6}}">${{esc(eventDecisionLabel(event))}}</text><text class="tree-node-subtitle" x="${{entry.x}}" y="${{entry.y + 12}}">${{esc(eventSubtitle(event))}}</text><text class="tree-node-label" x="${{entry.x}}" y="${{entry.y + 48}}">${{esc(compactTaskId(event.taskId))}} · e${{esc(event.eventIndex)}}</text></g>`;
}}
function renderTreeLayout(tree, focusInteractionId = "") {{
  const eventGraph = buildEventGraph(tree, focusInteractionId);
  const levels = [];
  const measured = measureTree(eventGraph, 0, levels);
  currentTreeIndex = indexEventNodes(eventGraph);
  const offsets = treeLevelOffsets(levels);
  const nodes = [], links = [];
  placeTree(measured, TREE.marginX, offsets, nodes, links);
  const width = Math.max(760, Math.ceil(measured.width + TREE.marginX * 2));
  const height = Math.ceil(offsets[offsets.length - 1] + TREE.marginY + 18);
  const paths = links.map(link => {{
    const startY = link.from.y + TREE.nodeRadius;
    const endY = link.to.y - TREE.nodeRadius;
    const forkY = Math.round(startY + (endY - startY) * 0.42);
    const labelX = Math.round((link.from.x + link.to.x) / 2);
    const labelY = Math.round(forkY - 8);
    return `<path class="tree-trunk" d="M ${{link.from.x}} ${{startY}} L ${{link.from.x}} ${{forkY}}"/><path class="tree-fork" d="M ${{Math.min(link.from.x, link.to.x)}} ${{forkY}} L ${{Math.max(link.from.x, link.to.x)}} ${{forkY}}"/><path class="tree-link" d="M ${{link.to.x}} ${{forkY}} L ${{link.to.x}} ${{endY}}"/><text class="tree-branch-label" x="${{labelX}}" y="${{labelY}}">delegate</text>`;
  }}).join("");
  const renderedNodes = nodes.map(renderTreeNode).join("");
  return `<div class="tree-canvas"><svg class="tree-graph" viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Delegation decision tree"><defs><marker id="tree-arrow-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>${{paths}}${{renderedNodes}}</svg></div>${{renderTreeInspector(focusedTreeNode(measured))}}`;
}}
function renderTreeView(tree, focusInteractionId = "") {{
  if (!tree) return '<div class="tree-empty">No delegation record for this task — it was never seen as a task_assigned target or a comment_mentioned target.</div>';
  return `<div class="tree-view" data-tree-mode="event"><p class="tree-legend">Event tree: each circle is one assignment or mention event. Same-task events form a sequence; forks show child task branches from the parent event.</p>${{renderTreeLayout(tree, focusInteractionId)}}</div>`;
}}
async function showTaskTree(taskId, focusInteractionId = "") {{
  const id = String(taskId || "").trim();
  if (!id) return;
  const frame = document.querySelector("#timeline-frame");
  const hint = document.querySelector("#timeline-hint");
  const backButton = document.querySelector("#tree-back-button");
  try {{
    const res = await fetch(`/api/task-tree?task_id=${{encodeURIComponent(id)}}`, {{cache:"no-store"}});
    const data = await res.json();
    timelineViewMode = "tree";
    if (backButton) backButton.classList.remove("is-hidden");
    if (hint) hint.textContent = res.ok ? `Delegation tree rooted at task ${{id}}'s top-most ancestor.` : (data.error || "No delegation record for this task.");
    if (frame) frame.innerHTML = res.ok ? renderTreeView(data.tree, focusInteractionId) : renderTreeView(null);
  }} catch (e) {{
    timelineViewMode = "tree";
    if (backButton) backButton.classList.remove("is-hidden");
    if (frame) frame.innerHTML = '<div class="tree-empty">Failed to load delegation tree.</div>';
  }}
}}
async function showTimelineView() {{
  timelineViewMode = "timeline";
  const hint = document.querySelector("#timeline-hint");
  const backButton = document.querySelector("#tree-back-button");
  if (backButton) backButton.classList.add("is-hidden");
  if (hint) hint.textContent = "Semantic graph: timestamped, vertically scrollable, and fullscreen when expanded. Click any arrow or task id to open its delegation tree.";
  await refresh();
}}
function bindTreeControls() {{
  const frame = document.querySelector("#timeline-frame");
  if (frame) frame.addEventListener("click", event => {{
    const treeNode = event.target.closest("[data-inspect-event]");
    if (treeNode) {{
      event.preventDefault();
      inspectTreeNode(treeNode.getAttribute("data-inspect-event"));
      return;
    }}
    const el = event.target.closest("[data-task-id]");
    const taskId = el && el.getAttribute("data-task-id");
    if (taskId) showTaskTree(taskId, el.getAttribute("data-interaction-id") || "");
  }});
  if (frame) frame.addEventListener("keydown", event => {{
    if (event.key !== "Enter" && event.key !== " ") return;
    const treeNode = event.target.closest("[data-inspect-event]");
    if (!treeNode) return;
    event.preventDefault();
    inspectTreeNode(treeNode.getAttribute("data-inspect-event"));
  }});
  const form = document.querySelector("#tree-search-form");
  if (form) form.onsubmit = event => {{
    event.preventDefault();
    const input = document.querySelector("#tree-search-input");
    if (input) showTaskTree(input.value);
  }};
  const backButton = document.querySelector("#tree-back-button");
  if (backButton) backButton.onclick = () => showTimelineView();
}}
async function refresh() {{
  const [events, timeline] = await Promise.all([getJson("/api/events?limit=25"), getJson("/api/timeline?limit=25")]);
  if (timelineViewMode === "timeline") {{
    document.querySelector("#timeline-frame").innerHTML = renderSvg(timeline.timeline || []);
  }}
  renderLedger(events.events || [], timeline.timeline || []);
}}
function renderLangfusePanel(traces, total) {{
  const byProfile = {{}};
  for (const t of (traces || [])) {{
    const profile = String(t.name || "").replace("hermes/", "") || "unknown";
    if (!byProfile[profile]) byProfile[profile] = [];
    byProfile[profile].push(t);
  }}
  const aggRows = Object.keys(byProfile).sort().map(profile => {{
    const sessions = byProfile[profile];
    const totalCost = sessions.reduce((s, t) => s + (t.totalCost || 0), 0);
    const avgCost = sessions.length ? (totalCost / sessions.length).toFixed(4) : "–";
    const avgCalls = sessions.length ? (sessions.reduce((s, t) => s + (t.observations ? t.observations.length : 0), 0) / sessions.length).toFixed(1) : "–";
    const avgLat = sessions.length ? (sessions.reduce((s, t) => s + (t.latency || 0), 0) / sessions.length).toFixed(0) + "s" : "–";
    return `<tr><td>${{esc(profile)}}</td><td>${{sessions.length}}</td><td>${{totalCost.toFixed(4)}}</td><td>${{avgCost}}</td><td>${{avgCalls}}</td><td>${{avgLat}}</td></tr>`;
  }}).join("") || '<tr><td colspan="6">No sessions yet</td></tr>';
  const traceRows = (traces || []).slice(0, 50).map(t => {{
    const profile = String(t.name || "").replace("hermes/", "");
    const cost = t.totalCost != null ? t.totalCost.toFixed(4) : "–";
    const calls = t.observations ? t.observations.length : "–";
    const lat = t.latency != null ? t.latency.toFixed(0) + "s" : "–";
    return `<tr><td>${{esc(formatTimestamp(t.timestamp))}}</td><td>${{esc(profile)}}</td><td>${{cost}}</td><td>${{calls}}</td><td>${{lat}}</td></tr>`;
  }}).join("") || '<tr><td colspan="5">No traces yet</td></tr>';
  const aggBody = document.querySelector("#lf-agg-body");
  const traceBody = document.querySelector("#lf-trace-body");
  const count = document.querySelector("#lf-count");
  if (aggBody) aggBody.innerHTML = aggRows;
  if (traceBody) traceBody.innerHTML = traceRows;
  if (count) count.textContent = `(${{(traces || []).length}} shown / ${{total}} total)`;
}}
async function fetchLangfuse() {{
  const statusEl = document.querySelector("#lf-status");
  try {{
    const data = await getJson("/api/langfuse?limit=50");
    if (statusEl) statusEl.textContent = "";
    if (!data.configured) {{
      if (statusEl) statusEl.textContent = data.error || "Langfuse not configured";
      const b = document.querySelector("#lf-agg-body");
      const t = document.querySelector("#lf-trace-body");
      if (b) b.innerHTML = '<tr><td colspan="6">–</td></tr>';
      if (t) t.innerHTML = '<tr><td colspan="6">–</td></tr>';
      return;
    }}
    if (data.error) {{
      if (statusEl) statusEl.textContent = "⚠ " + data.error;
    }}
    renderLangfusePanel(data.traces || [], data.total || 0);
  }} catch (e) {{
    if (statusEl) statusEl.textContent = "⚠ fetch failed";
  }}
}}
bindTimelineExpand();
bindToggles();
bindTreeControls();
fetchLangfuse();
setInterval(refresh, 5000);
setInterval(fetchLangfuse, 30000);
</script>
</body>
</html>
""".encode("utf-8")


def _authorized(headers: Mapping[str, str], token: str) -> bool:
    header_value = next((value for key, value in headers.items() if key.lower() == TOKEN_HEADER.lower()), "")
    return bool(token) and secrets.compare_digest(header_value, token)


def _ensure_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _set_enabled_gate(config: dict[str, Any], body: dict[str, Any]) -> tuple[bool, str]:
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return False, "enabled must be boolean"

    scope = body.get("scope")
    if scope == "global":
        key = body.get("key")
        if key not in {"forwarding_enabled", "due_poller_forwarding_enabled"}:
            return False, "unsupported global key"
        global_config = _ensure_dict(config.get("global"))
        global_config[key] = enabled
        config["global"] = global_config
        return True, f"global.{key}"

    if scope == "event":
        name = body.get("name")
        if not isinstance(name, str) or not name:
            return False, "event name required"
        events = _ensure_dict(config.get("events"))
        events[name] = enabled
        config["events"] = events
        return True, f"events.{name}"

    if scope == "agent":
        name = body.get("name")
        if not isinstance(name, str) or not name:
            return False, "agent name required"
        agents = _ensure_dict(config.get("agents"))
        agent_config = _ensure_dict(agents.get(name))
        agent_config["enabled"] = enabled
        agents[name] = agent_config
        config["agents"] = agents
        return True, f"agents.{name}.enabled"

    if scope == "agent_event":
        agent = body.get("agent")
        event = body.get("event")
        if not isinstance(agent, str) or not agent or not isinstance(event, str) or not event:
            return False, "agent and event required"
        agents = _ensure_dict(config.get("agents"))
        agent_config = _ensure_dict(agents.get(agent))
        events = _ensure_dict(agent_config.get("events"))
        events[event] = enabled
        agent_config["events"] = events
        agents[agent] = agent_config
        config["agents"] = agents
        return True, f"agents.{agent}.events.{event}"

    if scope == "project":
        name = body.get("name")
        if not isinstance(name, str) or not name:
            return False, "project name required"
        projects = _ensure_dict(config.get("projects"))
        project_config = _ensure_dict(projects.get(name))
        project_config["enabled"] = enabled
        projects[name] = project_config
        config["projects"] = projects
        return True, f"projects.{name}.enabled"

    if scope == "project_agent":
        project_id = body.get("project_id")
        agent = body.get("agent")
        if not isinstance(project_id, str) or not project_id or not isinstance(agent, str) or not agent:
            return False, "project_id and agent required"
        projects = _ensure_dict(config.get("projects"))
        project_config = _ensure_dict(projects.get(project_id))
        agents = _ensure_dict(project_config.get("agents"))
        agents[agent] = enabled
        project_config["agents"] = agents
        projects[project_id] = project_config
        config["projects"] = projects
        return True, f"projects.{project_id}.agents.{agent}"

    return False, "unsupported scope"


def _toggle_config(control_home: Path, raw_body: bytes) -> ApiResponse:
    try:
        body = json.loads(raw_body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(400, {"error": "invalid JSON"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "JSON object required"})

    config, status = _load_config(control_home)
    if status == "invalid":
        return _json_response(409, {"error": "config invalid", "config_status": status})

    ok, changed = _set_enabled_gate(config, body)
    if not ok:
        return _json_response(400, {"error": changed})

    path = control_config_path(control_home)
    _atomic_write_json(path, config)
    ledger = ControlLedger(control_home=control_home)
    ledger.initialize_schema()
    ledger.record_config_audit(
        action="toggle",
        status="updated",
        config_path=str(path),
        config_hash=payload_hash(config),
        reason=changed,
    )
    return _json_response(200, {"ok": True, "changed": changed, "config_status": "loaded"})


def handle_api_request(
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
    control_home: Path | None = None,
    token: str = "",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ApiResponse:
    headers = headers or {}
    control_home = control_home or resolve_control_home()
    parsed = urlparse(path)
    query = parse_qs(parsed.query)

    if method == "GET" and parsed.path in KNOWN_ASSETS:
        return ApiResponse(status=200, body=_control_page(control_home), content_type="text/html; charset=utf-8")

    if len(body) > MAX_BODY_BYTES:
        return _json_response(413, {"error": "payload too large"})

    if parsed.path == "/api/status":
        if method != "GET":
            return _json_response(405, {"error": "method not allowed"})
        return _json_response(200, _status(control_home, host, port))

    if parsed.path == "/api/config/effective":
        if method != "GET":
            return _json_response(405, {"error": "method not allowed"})
        return _json_response(200, _effective_config(control_home))

    if parsed.path == "/api/events":
        if method != "GET":
            return _json_response(405, {"error": "method not allowed"})
        return _json_response(200, {"limit": _bounded_limit(query), "events": _events(control_home, _bounded_limit(query))})

    if parsed.path == "/api/timeline":
        if method != "GET":
            return _json_response(405, {"error": "method not allowed"})
        limit = _bounded_limit(query)
        return _json_response(200, {"limit": limit, "timeline": _timeline(control_home, limit)})

    if parsed.path == "/api/task-tree":
        if method != "GET":
            return _json_response(405, {"error": "method not allowed"})
        task_id = (query.get("task_id", [""])[0] or "").strip()
        if not task_id:
            return _json_response(400, {"error": "task_id required"})
        tree = _build_task_tree(_task_delegation_rows(control_home), task_id)
        if tree is None:
            return _json_response(404, {"error": "no delegation record for task", "task_id": task_id})
        return _json_response(200, {"task_id": task_id, "tree": tree})

    if parsed.path == "/api/config/toggle":
        if method != "POST":
            return _json_response(405, {"error": "method not allowed"})
        if not _authorized(headers, token):
            return _json_response(403, {"error": "forbidden"})
        return _toggle_config(control_home, body)

    if parsed.path == "/api/langfuse":
        if method != "GET":
            return _json_response(405, {"error": "method not allowed"})
        limit = _bounded_limit(query)
        return _json_response(200, _fetch_langfuse_traces(limit))

    return _json_response(404, {"error": "not found"})


def resolve_token(control_home: Path | None = None) -> str:
    env_token = os.environ.get(TOKEN_ENV, "")
    if env_token:
        return env_token

    control_home = control_home or resolve_control_home()
    token_path = Path(os.environ.get(TOKEN_FILE_ENV, control_home / DEFAULT_TOKEN_FILE_NAME))
    try:
        existing = token_path.read_text().strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing

    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token + "\n")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return token


def make_handler(
    *,
    control_home: Path,
    token: str,
    host: str,
    port: int,
) -> type[BaseHTTPRequestHandler]:
    class ControlUiHandler(BaseHTTPRequestHandler):
        server_version = "TodoistControlUI/1.0"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            self._send(handle_api_request("GET", self.path, control_home=control_home, token=token, host=host, port=port))

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(min(length, MAX_BODY_BYTES + 1))
            self._send(
                handle_api_request(
                    "POST",
                    self.path,
                    body=raw_body,
                    headers={key: value for key, value in self.headers.items()},
                    control_home=control_home,
                    token=token,
                    host=host,
                    port=port,
                )
            )

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
            self._send(_json_response(405, {"error": "method not allowed"}))

        def log_message(self, format: str, *args: Any) -> None:
            return None

        def _send(self, response: ApiResponse) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for key, value in response.headers:
                self.send_header(key, value)
            try:
                self.end_headers()
                self.wfile.write(response.body)
            except (BrokenPipeError, ConnectionResetError):
                return None

    return ControlUiHandler


def create_server(
    *,
    port: int = DEFAULT_PORT,
    control_home: Path | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    host = DEFAULT_HOST
    control_home = control_home or resolve_control_home()
    token = token if token is not None else resolve_token(control_home)
    handler = make_handler(control_home=control_home, token=token, host=host, port=port)
    return ThreadingHTTPServer((host, port), handler)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local Todoist/Hermes control UI API server.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CONTROL_UI_PORT", str(DEFAULT_PORT))))
    parser.set_defaults(host=DEFAULT_HOST)
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()
    control_home = resolve_control_home()
    token = resolve_token(control_home)
    server = create_server(port=args.port, control_home=control_home, token=token)
    print(f"control UI API listening on http://{args.host}:{args.port}")
    print(f"token header: {TOKEN_HEADER}; token file/env configured outside responses")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
