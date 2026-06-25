#!/usr/bin/env python3
"""Local stdlib-only HTTP API for the Todoist/Hermes control surface.

The API is intentionally local and narrow: it serves a tiny embedded placeholder
asset set plus JSON endpoints backed only by CONTROL_HOME runtime files. It does
not read or edit Hermes-owned files under ``~/.hermes``.
"""

from __future__ import annotations

import argparse
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

KNOWN_ASSETS = {
    "/": (
        "text/html; charset=utf-8",
        b"""<!doctype html>
<html lang=\"en\">
<head><meta charset=\"utf-8\"><title>Todoist Hermes Control</title></head>
<body><main><h1>Todoist Hermes Control API</h1></main></body>
</html>
""",
    ),
    "/index.html": (
        "text/html; charset=utf-8",
        b"""<!doctype html>
<html lang=\"en\">
<head><meta charset=\"utf-8\"><title>Todoist Hermes Control</title></head>
<body><main><h1>Todoist Hermes Control API</h1></main></body>
</html>
""",
    ),
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
        content_type, asset = KNOWN_ASSETS[parsed.path]
        return ApiResponse(status=200, body=asset, content_type=content_type)

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
