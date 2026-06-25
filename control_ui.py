#!/usr/bin/env python3
"""Local stdlib-only HTTP API for the Todoist/Hermes control surface.

The API is intentionally local and narrow: it serves a tiny embedded placeholder
asset set plus JSON endpoints backed only by CONTROL_HOME runtime files. It does
not read or edit Hermes-owned files under ``~/.hermes``.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

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
AGENT_COLUMNS = ("Max", "Abra", "Smith", "Hausmeister", "System", "Unknown")
AGENT_KEYS = {name.lower(): name for name in AGENT_COLUMNS}

KNOWN_ASSETS = {
    "/": "control-page",
    "/index.html": "control-page",
}

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
            created_at AS occurred_at,
            COALESCE(actor, '') AS actor,
            COALESCE(target, agent, '') AS target,
            COALESCE(interaction_kind, interaction_type, '') AS interaction_kind,
            COALESCE(confidence, '') AS confidence,
            event_row_id AS event_id,
            status,
            reason
        FROM interactions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )


def _safe_text(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _agent_column(value: Any) -> str:
    key = str(value or "").strip().lower()
    return AGENT_KEYS.get(key, "Unknown")


def _timeline_y_positions(rows: list[dict[str, Any]], *, top: int, bottom: int, height: int) -> dict[int, int]:
    indexed_rows = list(enumerate(rows))
    ordered = sorted(indexed_rows, key=lambda item: str(item[1].get("occurred_at", "")))
    if len(ordered) <= 1:
        return {index: (top + height - bottom) // 2 for index, _ in ordered}
    chart_height = height - top - bottom
    last_index = len(ordered) - 1
    positions: dict[int, int] = {}
    for age_index, (original_index, _row) in enumerate(ordered):
        positions[original_index] = round(top + ((last_index - age_index) / last_index) * chart_height)
    return positions


def _timeline_state_class(row: Mapping[str, Any]) -> str:
    status = str(row.get("status", "")).lower()
    reason = str(row.get("reason", "")).lower()
    if "fail" in status or "failed" in reason or status.startswith(("http_4", "http_5")):
        return "failed"
    if status in {"suppressed", "deferred", "unrouted"} or "disabled" in reason or "record" in reason:
        return "disabled"
    return "forwarded"


def _render_timeline_svg(rows: list[dict[str, Any]]) -> str:
    width = 880
    height = 360
    top = 46
    bottom = 52
    left = 72
    right = 44
    step = (width - left - right) / (len(AGENT_COLUMNS) - 1)
    column_x = {agent: round(left + index * step) for index, agent in enumerate(AGENT_COLUMNS)}
    positions = _timeline_y_positions(rows, top=top, bottom=bottom, height=height)

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
        actor = _agent_column(row.get("actor"))
        target = _agent_column(row.get("target"))
        y = positions.get(index, (top + height - bottom) // 2)
        start_x = column_x[actor]
        end_x = column_x[target]
        curve = max(18, abs(end_x - start_x) // 3)
        state = _timeline_state_class(row)
        event_id = _safe_text(row.get("event_id"))
        kind = _safe_text(row.get("interaction_kind"))
        occurred_at = _safe_text(row.get("occurred_at"))
        arrow_markup.append(
            f'<path class="timeline-arrow {state}" data-event-id="{event_id}" '
            f'data-actor="{actor}" data-target="{target}" data-y="{y}" '
            f'd="M {start_x} {y} C {start_x + curve} {y - 18}, {end_x - curve} {y - 18}, {end_x} {y}" />'
        )
        arrow_markup.append(
            f'<circle class="timeline-dot {state}" cx="{start_x}" cy="{y}" r="4" />'
            f'<text class="timeline-label" x="{min(start_x, end_x) + 6}" y="{max(18, y - 24)}">'
            f'#{event_id} {kind} {occurred_at}</text>'
        )

    empty_markup = ""
    if not rows:
        empty_markup = '<text class="empty-state" x="440" y="180">No timeline rows yet</text>'

    return (
        f'<svg id="timeline-svg" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Todoist Hermes interaction timeline" data-testid="timeline-svg">'
        '<defs><marker id="arrow-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>'
        f'<line class="time-axis" x1="36" y1="{top}" x2="36" y2="{height - bottom}" />'
        f'<text class="axis-label" x="18" y="{top + 6}">new</text>'
        f'<text class="axis-label" x="16" y="{height - bottom}">old</text>'
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
<div class="control-toolbar">
  <label>Control token <input id="token-input" type="password" autocomplete="off" placeholder="paste local token" /></label>
  <output id="write-status">read-only until a token is entered</output>
</div>
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
        f'<td>{_safe_text(row.get("status"))}</td>'
        f'<td>{_safe_text(row.get("reason"))}</td>'
        "</tr>"
        for row in timeline
    ) or '<tr><td colspan="5">No routing outcomes recorded yet</td></tr>'
    return f"""
<div class="ledger-grid">
  <div class="panel"><h3>Recent events</h3><table id="events-table"><thead><tr><th>id</th><th>event</th><th>source</th><th>project</th><th>agent</th><th>received</th></tr></thead><tbody>{event_rows}</tbody></table></div>
  <div class="panel"><h3>Routing outcomes</h3><table id="outcomes-table"><thead><tr><th>event</th><th>route</th><th>kind</th><th>status</th><th>reason</th></tr></thead><tbody>{outcome_rows}</tbody></table></div>
</div>
"""


def _control_page(control_home: Path) -> bytes:
    config = _effective_config(control_home)
    events = _events(control_home, DEFAULT_LIMIT)
    timeline = _timeline(control_home, DEFAULT_LIMIT)
    controls = _render_controls(config)
    timeline_svg = _render_timeline_svg(timeline)
    ledger = _render_event_ledger(events, timeline)
    status = _safe_text(config.get("config_status", "unknown"))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Todoist Hermes Control</title>
<style>
:root {{ --ink:#d7ff9d; --text:#e6e6dc; --muted:#8b927f; --line:#39402f; --panel:#12150f; --panel-2:#181c13; --bg:#090b07; --red:#ff6b6b; --amber:#ffd166; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:13px/1.5 "SF Mono", "Geist Mono", "JetBrains Mono", Consolas, monospace; }}
main {{ max-width:1180px; margin:0 auto; padding:24px; }}
header {{ border:1px solid var(--line); background:var(--panel); padding:16px; margin-bottom:14px; }}
h1,h2,h3,p {{ margin:0; }}
h1 {{ color:var(--ink); font-size:18px; letter-spacing:.08em; text-transform:uppercase; }}
h2 {{ color:var(--ink); font-size:15px; margin-bottom:10px; }}
h3 {{ font-size:12px; color:var(--amber); margin-bottom:10px; text-transform:uppercase; letter-spacing:.08em; }}
.status-line {{ color:var(--muted); margin-top:6px; }}
.tabs {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:14px; }}
.tabs a {{ color:var(--text); text-decoration:none; border:1px solid var(--line); background:var(--panel-2); padding:8px 10px; }}
section[data-main-section] {{ border:1px solid var(--line); background:#0d100b; padding:14px; margin-bottom:14px; }}
.control-toolbar,.control-grid,.ledger-grid {{ display:grid; gap:10px; }}
.control-toolbar {{ grid-template-columns:1fr 1fr; align-items:end; margin-bottom:10px; }}
.control-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
.ledger-grid {{ grid-template-columns:1fr; }}
.panel {{ border:1px solid var(--line); background:var(--panel); padding:12px; overflow:auto; }}
input {{ width:100%; color:var(--text); background:#080a06; border:1px solid var(--line); padding:7px; font:inherit; }}
button {{ color:var(--ink); background:#0a0d08; border:1px solid var(--line); padding:7px 8px; font:inherit; cursor:pointer; }}
button:hover {{ border-color:var(--ink); }}
.toggle-grid {{ display:grid; gap:6px; }}
.toggle {{ display:flex; justify-content:space-between; gap:10px; text-align:left; }}
.toggle b {{ color:var(--muted); font-weight:400; }}
.toggle.is-enabled b {{ color:var(--ink); }}
.toggle.is-disabled b {{ color:var(--red); }}
.inline-form {{ display:grid; grid-template-columns:1fr auto auto; gap:8px; align-items:center; margin-top:10px; }}
.hint,.empty-state,.axis-label {{ fill:var(--muted); color:var(--muted); }}
svg {{ width:100%; min-height:360px; border:1px solid var(--line); background:#090b07; }}
.agent-column line,.time-axis {{ stroke:var(--line); stroke-width:1; }}
.agent-column text,.timeline-label {{ fill:var(--muted); font-size:11px; text-anchor:middle; }}
.timeline-label {{ text-anchor:start; }}
#arrow-head path {{ fill:var(--ink); }}
.timeline-arrow {{ fill:none; stroke:var(--ink); stroke-width:2; marker-end:url(#arrow-head); }}
.timeline-arrow.disabled {{ stroke:var(--muted); stroke-dasharray:5 5; }}
.timeline-arrow.failed {{ stroke:var(--red); }}
.timeline-dot {{ fill:var(--ink); }}
.timeline-dot.disabled {{ fill:var(--muted); }}
.timeline-dot.failed {{ fill:var(--red); }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th,td {{ border-bottom:1px solid var(--line); padding:6px; text-align:left; vertical-align:top; }}
th {{ color:var(--amber); font-weight:400; }}
@media (max-width:760px) {{ .control-grid,.control-toolbar,.tabs {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<main>
<header><h1>Todoist Hermes Control</h1><p class="status-line">local-only control surface / config: {status} / token stays outside API responses</p></header>
<nav class="tabs" aria-label="Main sections"><a href="#controls">Controls</a><a href="#timeline">Timeline</a><a href="#event-ledger">Event ledger</a></nav>
<section id="controls" data-main-section="Controls"><h2>Controls</h2>{controls}</section>
<section id="timeline" data-main-section="Timeline"><h2>Timeline</h2><div id="timeline-frame">{timeline_svg}</div></section>
<section id="event-ledger" data-main-section="Event ledger"><h2>Event ledger</h2><div id="ledger-frame">{ledger}</div></section>
</main>
<script>
const TOKEN_HEADER = "{TOKEN_HEADER}";
const AGENT_COLUMNS = {json.dumps(AGENT_COLUMNS)};
const esc = value => String(value ?? "").replace(/[&<>\"]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c]));
const agentColumn = value => AGENT_COLUMNS.find(name => name.toLowerCase() === String(value || "").toLowerCase()) || "Unknown";
async function getJson(url) {{ const res = await fetch(url, {{cache:"no-store"}}); return res.json(); }}
async function postToggle(payload) {{
  const token = document.querySelector("#token-input").value;
  const out = document.querySelector("#write-status");
  const res = await fetch("/api/config/toggle", {{method:"POST", headers:{{"Content-Type":"application/json", [TOKEN_HEADER]:token}}, body:JSON.stringify(payload)}});
  const data = await res.json();
  out.textContent = res.ok ? `updated ${{data.changed}}` : `write failed: ${{data.error || res.status}}`;
  if (res.ok) window.location.reload();
}}
function renderLedger(events, timeline) {{
  const eventRows = (events || []).map(row => `<tr><td>${{esc(row.id)}}</td><td>${{esc(row.event_name)}}</td><td>${{esc(row.source)}}</td><td>${{esc(row.project_id)}}</td><td>${{esc(row.agent)}}</td><td>${{esc(row.received_at)}}</td></tr>`).join("") || '<tr><td colspan="6">No events recorded yet</td></tr>';
  const outcomeRows = (timeline || []).map(row => `<tr><td>${{esc(row.event_id)}}</td><td>${{esc(row.actor)}} -> ${{esc(row.target)}}</td><td>${{esc(row.interaction_kind)}}</td><td>${{esc(row.status)}}</td><td>${{esc(row.reason)}}</td></tr>`).join("") || '<tr><td colspan="5">No routing outcomes recorded yet</td></tr>';
  document.querySelector("#events-table tbody").innerHTML = eventRows;
  document.querySelector("#outcomes-table tbody").innerHTML = outcomeRows;
}}
function renderSvg(rows) {{
  const width = 880, height = 360, top = 46, bottom = 52, left = 72, right = 44;
  const step = (width - left - right) / (AGENT_COLUMNS.length - 1);
  const x = Object.fromEntries(AGENT_COLUMNS.map((name, i) => [name, Math.round(left + i * step)]));
  const ordered = rows.map((row, index) => [row, index]).sort((a,b) => String(a[0].occurred_at || "").localeCompare(String(b[0].occurred_at || "")));
  const yByIndex = {{}};
  ordered.forEach((pair, ageIndex) => {{ yByIndex[pair[1]] = ordered.length <= 1 ? Math.round((top + height - bottom) / 2) : Math.round(top + (((ordered.length - 1 - ageIndex) / (ordered.length - 1)) * (height - top - bottom))); }});
  const columns = AGENT_COLUMNS.map(name => `<g class="agent-column" data-agent="${{name}}"><line x1="${{x[name]}}" y1="${{top}}" x2="${{x[name]}}" y2="${{height-bottom}}"/><text x="${{x[name]}}" y="${{height-18}}">${{name}}</text></g>`).join("");
  const arrows = rows.map((row, index) => {{
    const actor = agentColumn(row.actor), target = agentColumn(row.target), y = yByIndex[index], start = x[actor], end = x[target];
    const status = String(row.status || "").toLowerCase(), reason = String(row.reason || "").toLowerCase();
    const state = status.includes("fail") || reason.includes("failed") || status.startsWith("http_4") || status.startsWith("http_5") ? "failed" : (status === "suppressed" || status === "deferred" || status === "unrouted" || reason.includes("disabled") || reason.includes("record") ? "disabled" : "forwarded");
    const curve = Math.max(18, Math.floor(Math.abs(end - start) / 3));
    return `<path class="timeline-arrow ${{state}}" data-event-id="${{esc(row.event_id)}}" data-actor="${{actor}}" data-target="${{target}}" data-y="${{y}}" d="M ${{start}} ${{y}} C ${{start+curve}} ${{y-18}}, ${{end-curve}} ${{y-18}}, ${{end}} ${{y}}"/><circle class="timeline-dot ${{state}}" cx="${{start}}" cy="${{y}}" r="4"/>`;
  }}).join("");
  return `<svg id="timeline-svg" viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Todoist Hermes interaction timeline" data-testid="timeline-svg"><defs><marker id="arrow-head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs><line class="time-axis" x1="36" y1="${{top}}" x2="36" y2="${{height-bottom}}"/><text class="axis-label" x="18" y="${{top+6}}">new</text><text class="axis-label" x="16" y="${{height-bottom}}">old</text>${{columns}}${{arrows || '<text class="empty-state" x="440" y="180">No timeline rows yet</text>'}}</svg>`;
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
async function refresh() {{
  const [events, timeline] = await Promise.all([getJson("/api/events?limit=25"), getJson("/api/timeline?limit=25")]);
  document.querySelector("#timeline-frame").innerHTML = renderSvg(timeline.timeline || []);
  renderLedger(events.events || [], timeline.timeline || []);
}}
bindToggles();
setInterval(refresh, 5000);
</script>
</body>
</html>
""".encode("utf-8")


def _authorized(headers: Mapping[str, str], token: str) -> bool:
    header_value = headers.get(TOKEN_HEADER, "")
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

    if parsed.path == "/api/config/toggle":
        if method != "POST":
            return _json_response(405, {"error": "method not allowed"})
        if not _authorized(headers, token):
            return _json_response(403, {"error": "forbidden"})
        return _toggle_config(control_home, body)

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
            self.end_headers()
            self.wfile.write(response.body)

    return ControlUiHandler


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    control_home: Path | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    control_home = control_home or resolve_control_home()
    token = token if token is not None else resolve_token(control_home)
    handler = make_handler(control_home=control_home, token=token, host=host, port=port)
    return ThreadingHTTPServer((host, port), handler)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local Todoist/Hermes control UI API server.")
    parser.add_argument("--host", default=os.environ.get("CONTROL_UI_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CONTROL_UI_PORT", str(DEFAULT_PORT))))
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()
    control_home = resolve_control_home()
    token = resolve_token(control_home)
    server = create_server(host=args.host, port=args.port, control_home=control_home, token=token)
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
