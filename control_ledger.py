"""Runtime control gates and best-effort interaction ledger helpers.

This module is intentionally independent from ``proxy.py`` and
``due_poller.py`` for now. Later integration can call ``evaluate_forwarding``
before delivery and optionally record decisions through ``ControlLedger``.

Invalid or missing ``todoist-control.json`` preserves the current production
compatibility default: forwarding remains enabled unless a caller explicitly
passes the legacy ``todoist-proxy.disabled`` sentinel path and it exists. The
ledger never stores raw payload bodies; it records normalized fields and
deterministic SHA-256 hashes instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONTROL_HOME = Path("/home/filipp/todoist-hermes-control")
CONTROL_CONFIG_NAME = "todoist-control.json"
LEDGER_DB_NAME = "todoist_interactions.db"
BUSY_TIMEOUT_MS = 5000
INTERACTION_TIMELINE_COLUMNS = {
    "actor": "TEXT",
    "target": "TEXT",
    "interaction_kind": "TEXT",
    "confidence": "TEXT",
}


@dataclass(frozen=True)
class ControlDecision:
    """Explicit forwarding decision data for proxy/poller integration."""

    enabled: bool
    reason: str
    source: str
    event_name: str
    project_id: str
    agent: str
    config_status: str
    config_path: str


@dataclass(frozen=True)
class LedgerResult:
    """Return value for best-effort ledger operations."""

    success: bool
    reason: str
    row_id: int | None = None
    payload_hash: str | None = None
    error: str | None = None


def resolve_control_home() -> Path:
    """Return CONTROL_HOME or the dedicated default control directory."""

    return Path(os.environ.get("CONTROL_HOME", DEFAULT_CONTROL_HOME))


def control_config_path(control_home: Path | None = None) -> Path:
    """Return the control JSON path without touching the filesystem."""

    return (control_home or resolve_control_home()) / CONTROL_CONFIG_NAME


def ledger_db_path(control_home: Path | None = None) -> Path:
    """Return the interaction ledger DB path without touching the filesystem."""

    return (control_home or resolve_control_home()) / LEDGER_DB_NAME


def payload_hash(payload: Any) -> str:
    """Hash a normalized JSON representation of a payload-like object."""

    normalized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config(path: Path) -> tuple[dict[str, Any], str]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, "missing"
    except (json.JSONDecodeError, OSError):
        return {}, "invalid"
    if not isinstance(data, dict):
        return {}, "invalid"
    return data, "loaded"


def _gate_enabled(value: Any) -> bool:
    """Interpret bool gates and dict gates with an ``enabled`` field."""

    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
        return bool(value["enabled"])
    return True


def _decision(
    *,
    enabled: bool,
    reason: str,
    source: str,
    event_name: str,
    project_id: str,
    agent: str,
    config_status: str,
    config_path_value: Path,
) -> ControlDecision:
    return ControlDecision(
        enabled=enabled,
        reason=reason,
        source=source,
        event_name=event_name,
        project_id=project_id,
        agent=agent,
        config_status=config_status,
        config_path=str(config_path_value),
    )


def evaluate_forwarding(
    *,
    event_name: str,
    project_id: str = "",
    agent: str = "",
    source: str = "proxy",
    control_home: Path | None = None,
    sentinel_path: Path | None = None,
) -> ControlDecision:
    """Evaluate runtime forwarding gates for one event/agent scope.

    Gate semantics are deliberately conservative and simple: every matching
    gate must allow forwarding. Missing scopes are treated as enabled.
    ``source='due_poller'`` additionally honors
    ``global.due_poller_forwarding_enabled``.
    """

    config_path_value = control_config_path(control_home)
    config, status = _load_config(config_path_value)

    if sentinel_path is not None and sentinel_path.exists():
        return _decision(
            enabled=False,
            reason="legacy_disable_sentinel_present",
            source=source,
            event_name=event_name,
            project_id=project_id,
            agent=agent,
            config_status=status,
            config_path_value=config_path_value,
        )

    if status == "missing":
        return _decision(
            enabled=True,
            reason="missing_config_forwarding_enabled",
            source=source,
            event_name=event_name,
            project_id=project_id,
            agent=agent,
            config_status=status,
            config_path_value=config_path_value,
        )
    if status == "invalid":
        return _decision(
            enabled=True,
            reason="invalid_config_forwarding_enabled",
            source=source,
            event_name=event_name,
            project_id=project_id,
            agent=agent,
            config_status=status,
            config_path_value=config_path_value,
        )

    global_config = config.get("global", {})
    if isinstance(global_config, dict):
        if global_config.get("forwarding_enabled") is False:
            return _decision(
                enabled=False,
                reason="global_forwarding_disabled",
                source=source,
                event_name=event_name,
                project_id=project_id,
                agent=agent,
                config_status=status,
                config_path_value=config_path_value,
            )
        if source == "due_poller" and global_config.get("due_poller_forwarding_enabled") is False:
            return _decision(
                enabled=False,
                reason="global_due_poller_forwarding_disabled",
                source=source,
                event_name=event_name,
                project_id=project_id,
                agent=agent,
                config_status=status,
                config_path_value=config_path_value,
            )

    events = config.get("events", {})
    if isinstance(events, dict) and events.get(event_name) is False:
        return _decision(
            enabled=False,
            reason=f"event_disabled:{event_name}",
            source=source,
            event_name=event_name,
            project_id=project_id,
            agent=agent,
            config_status=status,
            config_path_value=config_path_value,
        )

    project_config: Any = None
    projects = config.get("projects", {})
    if project_id and isinstance(projects, dict):
        project_config = projects.get(project_id)
        if project_config is not None and not _gate_enabled(project_config):
            return _decision(
                enabled=False,
                reason=f"project_disabled:{project_id}",
                source=source,
                event_name=event_name,
                project_id=project_id,
                agent=agent,
                config_status=status,
                config_path_value=config_path_value,
            )
        if agent and isinstance(project_config, dict):
            project_agents = project_config.get("agents", {})
            if isinstance(project_agents, dict) and project_agents.get(agent) is False:
                return _decision(
                    enabled=False,
                    reason=f"project_agent_disabled:{project_id}:{agent}",
                    source=source,
                    event_name=event_name,
                    project_id=project_id,
                    agent=agent,
                    config_status=status,
                    config_path_value=config_path_value,
                )

    agents = config.get("agents", {})
    if agent and isinstance(agents, dict):
        agent_config = agents.get(agent)
        if agent_config is not None and not _gate_enabled(agent_config):
            return _decision(
                enabled=False,
                reason=f"agent_disabled:{agent}",
                source=source,
                event_name=event_name,
                project_id=project_id,
                agent=agent,
                config_status=status,
                config_path_value=config_path_value,
            )
        if isinstance(agent_config, dict):
            agent_events = agent_config.get("events", {})
            if isinstance(agent_events, dict) and agent_events.get(event_name) is False:
                return _decision(
                    enabled=False,
                    reason=f"agent_event_disabled:{agent}:{event_name}",
                    source=source,
                    event_name=event_name,
                    project_id=project_id,
                    agent=agent,
                    config_status=status,
                    config_path_value=config_path_value,
                )

    return _decision(
        enabled=True,
        reason="forwarding_enabled",
        source=source,
        event_name=event_name,
        project_id=project_id,
        agent=agent,
        config_status=status,
        config_path_value=config_path_value,
    )


class ControlLedger:
    """Small SQLite ledger with nonfatal, best-effort write helpers."""

    def __init__(self, control_home: Path | None = None, db_path: Path | None = None) -> None:
        self.control_home = control_home or resolve_control_home()
        self.db_path = db_path or ledger_db_path(self.control_home)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize_schema(self) -> LedgerResult:
        try:
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_name TEXT NOT NULL,
                        source TEXT NOT NULL,
                        project_id TEXT,
                        agent TEXT,
                        todoist_task_id TEXT,
                        payload_hash TEXT NOT NULL,
                        received_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS routing_decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_row_id INTEGER,
                        event_name TEXT NOT NULL,
                        source TEXT NOT NULL,
                        project_id TEXT,
                        agent TEXT,
                        target TEXT,
                        enabled INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        config_status TEXT NOT NULL,
                        config_path TEXT NOT NULL,
                        decided_at TEXT NOT NULL,
                        FOREIGN KEY(event_row_id) REFERENCES events(id)
                    );

                    CREATE TABLE IF NOT EXISTS interactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_row_id INTEGER,
                        interaction_type TEXT NOT NULL,
                        actor TEXT,
                        agent TEXT,
                        target TEXT,
                        interaction_kind TEXT,
                        confidence TEXT,
                        project_id TEXT,
                        todoist_task_id TEXT,
                        payload_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reason TEXT,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(event_row_id) REFERENCES events(id)
                    );

                    CREATE TABLE IF NOT EXISTS config_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        config_path TEXT NOT NULL,
                        config_hash TEXT,
                        reason TEXT,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                self._ensure_interaction_timeline_columns(conn)
            return LedgerResult(success=True, reason="ok")
        except sqlite3.Error as exc:
            return LedgerResult(success=False, reason="sqlite_error", error=str(exc))
        except OSError as exc:
            return LedgerResult(success=False, reason="os_error", error=str(exc))

    def record_event(
        self,
        *,
        event_name: str,
        event_data: dict[str, Any],
        source: str = "proxy",
        agent: str = "",
    ) -> LedgerResult:
        digest = payload_hash(event_data)
        return self._insert(
            """
            INSERT INTO events (
                event_name, source, project_id, agent, todoist_task_id, payload_hash, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_name,
                source,
                str(event_data.get("project_id", "")),
                agent,
                str(event_data.get("id", "") or event_data.get("item_id", "")),
                digest,
                _utc_now(),
            ),
            payload_digest=digest,
        )

    def record_routing_decision(
        self,
        *,
        decision: ControlDecision,
        target: str = "",
        event_row_id: int | None = None,
    ) -> LedgerResult:
        return self._insert(
            """
            INSERT INTO routing_decisions (
                event_row_id, event_name, source, project_id, agent, target, enabled, reason,
                config_status, config_path, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_row_id,
                decision.event_name,
                decision.source,
                decision.project_id,
                decision.agent,
                target,
                1 if decision.enabled else 0,
                decision.reason,
                decision.config_status,
                decision.config_path,
                _utc_now(),
            ),
        )

    def record_interaction(
        self,
        *,
        interaction_type: str,
        agent: str,
        project_id: str,
        todoist_task_id: str,
        status: str,
        payload: Any,
        reason: str = "",
        event_row_id: int | None = None,
        actor: str = "",
        target: str = "",
        interaction_kind: str = "",
        confidence: str = "",
    ) -> LedgerResult:
        digest = payload_hash(payload)
        return self._insert(
            """
            INSERT INTO interactions (
                event_row_id, interaction_type, actor, agent, target, interaction_kind, confidence,
                project_id, todoist_task_id, payload_hash, status, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_row_id,
                interaction_type,
                actor,
                agent,
                target or agent,
                interaction_kind or interaction_type,
                confidence,
                project_id,
                todoist_task_id,
                digest,
                status,
                reason,
                _utc_now(),
            ),
            payload_digest=digest,
        )

    def _ensure_interaction_timeline_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(interactions)").fetchall()
        }
        for column, column_type in INTERACTION_TIMELINE_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE interactions ADD COLUMN {column} {column_type}")

    def record_config_audit(
        self,
        *,
        action: str,
        status: str,
        config_path: str,
        config_hash: str | None = None,
        reason: str = "",
    ) -> LedgerResult:
        return self._insert(
            """
            INSERT INTO config_audit (action, status, config_path, config_hash, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (action, status, config_path, config_hash, reason, _utc_now()),
        )

    def _insert(
        self,
        sql: str,
        params: tuple[Any, ...],
        *,
        payload_digest: str | None = None,
    ) -> LedgerResult:
        try:
            with self._connect() as conn:
                cursor = conn.execute(sql, params)
                row_id = cursor.lastrowid
            return LedgerResult(
                success=True,
                reason="ok",
                row_id=row_id,
                payload_hash=payload_digest,
            )
        except sqlite3.Error as exc:
            return LedgerResult(
                success=False,
                reason="sqlite_error",
                payload_hash=payload_digest,
                error=str(exc),
            )
        except OSError as exc:
            return LedgerResult(
                success=False,
                reason="os_error",
                payload_hash=payload_digest,
                error=str(exc),
            )
