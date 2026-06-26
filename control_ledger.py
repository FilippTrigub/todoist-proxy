"""Runtime control gates and best-effort interaction ledger helpers.

This module is intentionally independent from ``proxy.py`` and
``due_poller.py`` for now. Later integration can call ``evaluate_forwarding``
before delivery and optionally record decisions through ``ControlLedger``.

Invalid or missing ``todoist-control.json`` preserves the current production
compatibility default: forwarding remains enabled unless a caller explicitly
passes the legacy ``todoist-proxy.disabled`` sentinel path and it exists. Legacy
audit helpers never store raw payload bodies; the inbound ledger stores exact
request bytes for restart-safe ACK handling.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator


DEFAULT_CONTROL_HOME = Path("/home/filipp/todoist-hermes-control")
CONTROL_CONFIG_NAME = "todoist-control.json"
LEDGER_DB_NAME = "todoist_interactions.db"
BUSY_TIMEOUT_MS = 5000
INBOUND_HEADER_ALLOWLIST = (
    "X-Todoist-Hmac-SHA256",
    "X-Todoist-Delivery-ID",
    "Content-Type",
)
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


@dataclass(frozen=True)
class DeliveryIdentity:
    """Stable identity for one successful downstream delivery target."""

    source: str
    event_name: str
    entity_id: str
    parent_task_id: str
    due_value: str
    payload_hash: str
    subscription: str
    delivery_id: str = ""


@dataclass(frozen=True)
class PendingDelivery:
    """One queued downstream delivery or route-resolution attempt."""

    id: int
    inbound_event_id: int
    kind: str
    subscription: str | None
    state: str
    attempt_count: int
    next_attempt_at: str
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PendingDeliveryContext:
    """Stored inbound data needed to drain one pending delivery."""

    pending: PendingDelivery
    source: str
    event_name: str
    project_id: str
    delivery_id: str
    raw_body: bytes
    headers: dict[str, str]


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


def raw_payload_hash(raw_body: bytes) -> str:
    """Hash exact inbound request bytes."""

    return hashlib.sha256(raw_body).hexdigest()


def _allowlisted_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    lower_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    return {
        name: lower_headers[name.lower()]
        for name in INBOUND_HEADER_ALLOWLIST
        if name.lower() in lower_headers
    }


def _opaque_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text if text != "" else ""


def _first_opaque_id(data: dict[str, Any], *names: str) -> str:
    for name in names:
        text = _opaque_id(data.get(name))
        if text:
            return text
    return ""


def _event_due_value(event_data: dict[str, Any], explicit_due_value: Any) -> str:
    explicit = _opaque_id(explicit_due_value)
    if explicit:
        return explicit
    due = event_data.get("due")
    if isinstance(due, dict):
        return _first_opaque_id(due, "datetime", "date", "string")
    return _opaque_id(due)


def build_delivery_identity(
    *,
    source: str,
    event_name: str,
    event_data: dict[str, Any],
    subscription: str,
    delivery_id: str = "",
    due_value: Any = "",
    payload_digest: str | None = None,
    payload: Any | None = None,
) -> DeliveryIdentity:
    """Build the stable dedup identity for a successful target delivery.

    Todoist webhook callers should pass ``X-Todoist-Delivery-ID`` as
    ``delivery_id`` when available. That value is unique per notification and
    stable across Todoist retries, so lookup/write helpers prefer it over the
    fallback payload-derived identity. Due-poller callers can omit
    ``delivery_id`` and pass ``due_value`` so each recurrence due value remains
    independently deliverable per subscription.
    """

    digest = payload_digest or payload_hash(event_data if payload is None else payload)
    parent_task_id = _first_opaque_id(event_data, "item_id", "parent_task_id")
    if not parent_task_id and event_name != "note:added":
        parent_task_id = _first_opaque_id(event_data, "parent_id")
    return DeliveryIdentity(
        source=_opaque_id(source),
        event_name=_opaque_id(event_name),
        entity_id=_first_opaque_id(event_data, "id", "task_id", "item_id"),
        parent_task_id=parent_task_id,
        due_value=_event_due_value(event_data, due_value),
        payload_hash=digest,
        subscription=_opaque_id(subscription),
        delivery_id=_opaque_id(delivery_id),
    )


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

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

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

                    CREATE TABLE IF NOT EXISTS delivery_dedup (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source TEXT NOT NULL,
                        event_name TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        parent_task_id TEXT NOT NULL,
                        due_value TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        subscription TEXT NOT NULL,
                        delivery_id TEXT NOT NULL DEFAULT '',
                        delivered_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS inbound_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source TEXT NOT NULL DEFAULT 'proxy',
                        event_name TEXT NOT NULL,
                        entity_id TEXT,
                        project_id TEXT,
                        delivery_id TEXT,
                        payload_hash TEXT NOT NULL,
                        raw_body BLOB NOT NULL,
                        headers_json TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'accepted'
                    );

                    CREATE TABLE IF NOT EXISTS pending_deliveries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        inbound_event_id INTEGER NOT NULL REFERENCES inbound_events(id),
                        kind TEXT NOT NULL CHECK (kind IN ('delivery', 'routing_resolution')),
                        subscription TEXT,
                        state TEXT NOT NULL DEFAULT 'pending' CHECK (
                            state IN ('pending', 'retry', 'succeeded', 'terminal_failed', 'suppressed')
                        ),
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TEXT NOT NULL,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK (
                            (kind = 'delivery' AND subscription IS NOT NULL)
                            OR (kind = 'routing_resolution' AND subscription IS NULL)
                        )
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS delivery_dedup_delivery_id_idx
                    ON delivery_dedup (source, event_name, delivery_id, subscription)
                    WHERE delivery_id <> '';

                    CREATE UNIQUE INDEX IF NOT EXISTS delivery_dedup_fallback_idx
                    ON delivery_dedup (
                        source, event_name, entity_id, parent_task_id,
                        due_value, payload_hash, subscription
                    )
                    WHERE delivery_id = '';

                    CREATE UNIQUE INDEX IF NOT EXISTS inbound_events_delivery_id_idx
                    ON inbound_events (source, delivery_id)
                    WHERE delivery_id IS NOT NULL AND delivery_id != '';

                    CREATE UNIQUE INDEX IF NOT EXISTS inbound_events_fallback_idx
                    ON inbound_events (source, event_name, entity_id, project_id, payload_hash)
                    WHERE delivery_id IS NULL OR delivery_id = '';

                    CREATE UNIQUE INDEX IF NOT EXISTS pending_deliveries_delivery_idx
                    ON pending_deliveries (inbound_event_id, kind, subscription)
                    WHERE subscription IS NOT NULL;

                    CREATE UNIQUE INDEX IF NOT EXISTS pending_deliveries_routing_resolution_idx
                    ON pending_deliveries (inbound_event_id, kind)
                    WHERE kind = 'routing_resolution' AND subscription IS NULL;

                    CREATE INDEX IF NOT EXISTS pending_deliveries_due_idx
                    ON pending_deliveries (state, next_attempt_at);
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

    def record_inbound_event(
        self,
        *,
        event_name: str,
        event_data: dict[str, Any],
        raw_body: bytes,
        headers: Mapping[str, Any],
        source: str = "proxy",
        status: str = "accepted",
    ) -> LedgerResult:
        digest = raw_payload_hash(raw_body)
        allowed_headers = _allowlisted_headers(headers)
        delivery_id = _opaque_id(allowed_headers.get("X-Todoist-Delivery-ID", ""))
        entity_id = _first_opaque_id(event_data, "id", "task_id", "item_id")
        project_id = _opaque_id(event_data.get("project_id"))
        headers_json = json.dumps(allowed_headers, sort_keys=True, separators=(",", ":"))
        try:
            with self._connect() as conn:
                row_id, reason = self._insert_inbound_event(
                    conn,
                    source=_opaque_id(source),
                    event_name=_opaque_id(event_name),
                    entity_id=entity_id,
                    project_id=project_id,
                    delivery_id=delivery_id,
                    payload_digest=digest,
                    raw_body=raw_body,
                    headers_json=headers_json,
                    status=_opaque_id(status) or "accepted",
                )
                return LedgerResult(
                    success=True,
                    reason=reason,
                    row_id=row_id,
                    payload_hash=digest,
                )
        except sqlite3.Error as exc:
            return LedgerResult(
                success=False,
                reason="sqlite_error",
                payload_hash=digest,
                error=str(exc),
            )
        except OSError as exc:
            return LedgerResult(
                success=False,
                reason="os_error",
                payload_hash=digest,
                error=str(exc),
            )

    def record_inbound_event_and_enqueue_pending(
        self,
        *,
        event_name: str,
        event_data: dict[str, Any],
        raw_body: bytes,
        headers: Mapping[str, Any],
        kind: str,
        subscription: str | None = None,
        source: str = "proxy",
        status: str = "accepted",
        next_attempt_at: str | None = None,
    ) -> LedgerResult:
        pending_kind = _opaque_id(kind)
        pending_subscription = _opaque_id(subscription)
        if pending_kind == "delivery":
            if not pending_subscription:
                return LedgerResult(success=False, reason="invalid_pending_work")
        elif pending_kind == "routing_resolution":
            pending_subscription = None
        else:
            return LedgerResult(success=False, reason="invalid_pending_work")

        digest = raw_payload_hash(raw_body)
        allowed_headers = _allowlisted_headers(headers)
        delivery_id = _opaque_id(allowed_headers.get("X-Todoist-Delivery-ID", ""))
        entity_id = _first_opaque_id(event_data, "id", "task_id", "item_id")
        project_id = _opaque_id(event_data.get("project_id"))
        headers_json = json.dumps(allowed_headers, sort_keys=True, separators=(",", ":"))
        now = _utc_now()
        try:
            with self._connect() as conn:
                inbound_event_id, _ = self._insert_inbound_event(
                    conn,
                    source=_opaque_id(source),
                    event_name=_opaque_id(event_name),
                    entity_id=entity_id,
                    project_id=project_id,
                    delivery_id=delivery_id,
                    payload_digest=digest,
                    raw_body=raw_body,
                    headers_json=headers_json,
                    status=_opaque_id(status) or "accepted",
                )
                pending_id, reason = self._insert_pending_delivery(
                    conn,
                    inbound_event_id=inbound_event_id,
                    kind=pending_kind,
                    subscription=pending_subscription,
                    next_attempt_at=next_attempt_at or now,
                    now=now,
                )
                return LedgerResult(
                    success=True,
                    reason=reason,
                    row_id=pending_id,
                    payload_hash=digest,
                )
        except sqlite3.Error as exc:
            return LedgerResult(
                success=False,
                reason="sqlite_error",
                payload_hash=digest,
                error=str(exc),
            )
        except OSError as exc:
            return LedgerResult(
                success=False,
                reason="os_error",
                payload_hash=digest,
                error=str(exc),
            )

    def record_inbound_event_and_enqueue_pending_deliveries(
        self,
        *,
        event_name: str,
        event_data: dict[str, Any],
        raw_body: bytes,
        headers: Mapping[str, Any],
        subscriptions: Iterable[str],
        source: str = "proxy",
        status: str = "accepted",
        next_attempt_at: str | None = None,
    ) -> LedgerResult:
        pending_subscriptions = tuple(_opaque_id(subscription) for subscription in subscriptions)
        if not pending_subscriptions or any(not subscription for subscription in pending_subscriptions):
            return LedgerResult(success=False, reason="invalid_pending_work")

        digest = raw_payload_hash(raw_body)
        allowed_headers = _allowlisted_headers(headers)
        delivery_id = _opaque_id(allowed_headers.get("X-Todoist-Delivery-ID", ""))
        entity_id = _first_opaque_id(event_data, "id", "task_id", "item_id")
        project_id = _opaque_id(event_data.get("project_id"))
        headers_json = json.dumps(allowed_headers, sort_keys=True, separators=(",", ":"))
        now = _utc_now()
        reason = "already_pending"
        try:
            with self._connect() as conn:
                inbound_event_id, _ = self._insert_inbound_event(
                    conn,
                    source=_opaque_id(source),
                    event_name=_opaque_id(event_name),
                    entity_id=entity_id,
                    project_id=project_id,
                    delivery_id=delivery_id,
                    payload_digest=digest,
                    raw_body=raw_body,
                    headers_json=headers_json,
                    status=_opaque_id(status) or "accepted",
                )
                for subscription in pending_subscriptions:
                    _, pending_reason = self._insert_pending_delivery(
                        conn,
                        inbound_event_id=inbound_event_id,
                        kind="delivery",
                        subscription=subscription,
                        next_attempt_at=next_attempt_at or now,
                        now=now,
                    )
                    if pending_reason == "ok":
                        reason = "ok"
                return LedgerResult(
                    success=True,
                    reason=reason,
                    row_id=inbound_event_id,
                    payload_hash=digest,
                )
        except sqlite3.Error as exc:
            return LedgerResult(
                success=False,
                reason="sqlite_error",
                payload_hash=digest,
                error=str(exc),
            )
        except OSError as exc:
            return LedgerResult(
                success=False,
                reason="os_error",
                payload_hash=digest,
                error=str(exc),
            )

    def enqueue_pending_deliveries_for_inbound(
        self,
        *,
        inbound_event_id: int,
        subscriptions: Iterable[str],
        next_attempt_at: str | None = None,
    ) -> LedgerResult:
        pending_subscriptions = tuple(_opaque_id(subscription) for subscription in subscriptions)
        if not pending_subscriptions or any(not subscription for subscription in pending_subscriptions):
            return LedgerResult(success=False, reason="invalid_pending_work")

        now = _utc_now()
        reason = "already_pending"
        row_id: int | None = None
        try:
            with self._connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM inbound_events WHERE id = ?",
                    (inbound_event_id,),
                ).fetchone()
                if exists is None:
                    return LedgerResult(success=False, reason="inbound_not_found")
                for subscription in pending_subscriptions:
                    pending_id, pending_reason = self._insert_pending_delivery(
                        conn,
                        inbound_event_id=inbound_event_id,
                        kind="delivery",
                        subscription=subscription,
                        next_attempt_at=next_attempt_at or now,
                        now=now,
                    )
                    row_id = pending_id
                    if pending_reason == "ok":
                        reason = "ok"
                return LedgerResult(success=True, reason=reason, row_id=row_id)
        except sqlite3.Error as exc:
            return LedgerResult(success=False, reason="sqlite_error", error=str(exc))
        except OSError as exc:
            return LedgerResult(success=False, reason="os_error", error=str(exc))

    def pending_queue_depth(self) -> int:
        try:
            with self._connect() as conn:
                return int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM pending_deliveries
                        WHERE state IN ('pending', 'retry')
                        """
                    ).fetchone()[0]
                )
        except (sqlite3.Error, OSError):
            return 0

    def due_pending_deliveries(
        self,
        *,
        now: str | None = None,
        limit: int | None = None,
    ) -> list[PendingDelivery]:
        query = """
            SELECT id, inbound_event_id, kind, subscription, state, attempt_count,
                   next_attempt_at, last_error, created_at, updated_at
            FROM pending_deliveries
            WHERE state IN ('pending', 'retry') AND next_attempt_at <= ?
            ORDER BY next_attempt_at, id
        """
        params: tuple[Any, ...] = (now or _utc_now(),)
        if limit is not None:
            query += " LIMIT ?"
            params = (*params, limit)
        try:
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
        except (sqlite3.Error, OSError):
            return []
        return [
            PendingDelivery(
                id=int(row[0]),
                inbound_event_id=int(row[1]),
                kind=str(row[2]),
                subscription=row[3],
                state=str(row[4]),
                attempt_count=int(row[5]),
                next_attempt_at=str(row[6]),
                last_error=row[7],
                created_at=str(row[8]),
                updated_at=str(row[9]),
            )
            for row in rows
        ]

    def pending_delivery_context(self, pending_id: int) -> PendingDeliveryContext | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT p.id, p.inbound_event_id, p.kind, p.subscription, p.state,
                           p.attempt_count, p.next_attempt_at, p.last_error,
                           p.created_at, p.updated_at, i.source, i.event_name,
                           COALESCE(i.project_id, ''), COALESCE(i.delivery_id, ''),
                           i.raw_body, i.headers_json
                    FROM pending_deliveries p
                    JOIN inbound_events i ON i.id = p.inbound_event_id
                    WHERE p.id = ?
                    """,
                    (pending_id,),
                ).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if row is None:
            return None
        try:
            headers = json.loads(str(row[15]))
        except json.JSONDecodeError:
            headers = {}
        return PendingDeliveryContext(
            pending=PendingDelivery(
                id=int(row[0]),
                inbound_event_id=int(row[1]),
                kind=str(row[2]),
                subscription=row[3],
                state=str(row[4]),
                attempt_count=int(row[5]),
                next_attempt_at=str(row[6]),
                last_error=row[7],
                created_at=str(row[8]),
                updated_at=str(row[9]),
            ),
            source=str(row[10]),
            event_name=str(row[11]),
            project_id=str(row[12]),
            delivery_id=str(row[13]),
            raw_body=bytes(row[14]),
            headers={str(key): str(value) for key, value in headers.items()},
        )

    def update_pending_delivery_state(
        self,
        pending_id: int,
        *,
        state: str,
        last_error: str = "",
        next_attempt_at: str | None = None,
        increment_attempt: bool = False,
    ) -> LedgerResult:
        pending_state = _opaque_id(state)
        if pending_state not in {"pending", "retry", "succeeded", "terminal_failed", "suppressed"}:
            return LedgerResult(success=False, reason="invalid_pending_state")
        now = _utc_now()
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE pending_deliveries
                    SET state = ?,
                        attempt_count = attempt_count + ?,
                        next_attempt_at = COALESCE(?, next_attempt_at),
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        pending_state,
                        1 if increment_attempt else 0,
                        next_attempt_at,
                        _opaque_id(last_error) or None,
                        now,
                        pending_id,
                    ),
                )
            return LedgerResult(
                success=cursor.rowcount == 1,
                reason="ok" if cursor.rowcount == 1 else "not_found",
                row_id=pending_id if cursor.rowcount == 1 else None,
            )
        except sqlite3.Error as exc:
            return LedgerResult(success=False, reason="sqlite_error", error=str(exc))
        except OSError as exc:
            return LedgerResult(success=False, reason="os_error", error=str(exc))

    def has_successful_delivery(
        self,
        *,
        source: str,
        event_name: str,
        event_data: dict[str, Any],
        subscription: str,
        delivery_id: str = "",
        due_value: Any = "",
        payload_digest: str | None = None,
        payload: Any | None = None,
    ) -> bool:
        identity = build_delivery_identity(
            source=source,
            event_name=event_name,
            event_data=event_data,
            subscription=subscription,
            delivery_id=delivery_id,
            due_value=due_value,
            payload_digest=payload_digest,
            payload=payload,
        )
        try:
            with self._connect() as conn:
                return self._delivery_exists(conn, identity)
        except (sqlite3.Error, OSError):
            return False

    def record_successful_delivery(
        self,
        *,
        source: str,
        event_name: str,
        event_data: dict[str, Any],
        subscription: str,
        delivery_id: str = "",
        due_value: Any = "",
        payload_digest: str | None = None,
        payload: Any | None = None,
    ) -> LedgerResult:
        identity = build_delivery_identity(
            source=source,
            event_name=event_name,
            event_data=event_data,
            subscription=subscription,
            delivery_id=delivery_id,
            due_value=due_value,
            payload_digest=payload_digest,
            payload=payload,
        )
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO delivery_dedup (
                        source, event_name, entity_id, parent_task_id, due_value,
                        payload_hash, subscription, delivery_id, delivered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity.source,
                        identity.event_name,
                        identity.entity_id,
                        identity.parent_task_id,
                        identity.due_value,
                        identity.payload_hash,
                        identity.subscription,
                        identity.delivery_id,
                        _utc_now(),
                    ),
                )
                if cursor.rowcount == 1:
                    return LedgerResult(
                        success=True,
                        reason="ok",
                        row_id=cursor.lastrowid,
                        payload_hash=identity.payload_hash,
                    )
                row_id = self._delivery_row_id(conn, identity)
                return LedgerResult(
                    success=True,
                    reason="already_delivered",
                    row_id=row_id,
                    payload_hash=identity.payload_hash,
                )
        except sqlite3.Error as exc:
            return LedgerResult(
                success=False,
                reason="sqlite_error",
                payload_hash=identity.payload_hash,
                error=str(exc),
            )
        except OSError as exc:
            return LedgerResult(
                success=False,
                reason="os_error",
                payload_hash=identity.payload_hash,
                error=str(exc),
            )

    def _delivery_exists(self, conn: sqlite3.Connection, identity: DeliveryIdentity) -> bool:
        return self._delivery_row_id(conn, identity) is not None

    def _delivery_row_id(self, conn: sqlite3.Connection, identity: DeliveryIdentity) -> int | None:
        if identity.delivery_id:
            row = conn.execute(
                """
                SELECT id FROM delivery_dedup
                WHERE source = ? AND event_name = ? AND delivery_id = ? AND subscription = ?
                """,
                (identity.source, identity.event_name, identity.delivery_id, identity.subscription),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id FROM delivery_dedup
                WHERE source = ? AND event_name = ? AND entity_id = ? AND parent_task_id = ?
                    AND due_value = ? AND payload_hash = ? AND subscription = ? AND delivery_id = ''
                """,
                (
                    identity.source,
                    identity.event_name,
                    identity.entity_id,
                    identity.parent_task_id,
                    identity.due_value,
                    identity.payload_hash,
                    identity.subscription,
                ),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def _inbound_row_id(
        self,
        conn: sqlite3.Connection,
        *,
        source: str,
        event_name: str,
        entity_id: str,
        project_id: str,
        delivery_id: str,
        payload_digest: str,
    ) -> int | None:
        if delivery_id:
            row = conn.execute(
                """
                SELECT id FROM inbound_events
                WHERE source = ? AND delivery_id = ?
                """,
                (source, delivery_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id FROM inbound_events
                WHERE source = ? AND event_name = ? AND entity_id = ? AND project_id = ?
                    AND payload_hash = ? AND (delivery_id IS NULL OR delivery_id = '')
                """,
                (source, event_name, entity_id, project_id, payload_digest),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def _insert_inbound_event(
        self,
        conn: sqlite3.Connection,
        *,
        source: str,
        event_name: str,
        entity_id: str,
        project_id: str,
        delivery_id: str,
        payload_digest: str,
        raw_body: bytes,
        headers_json: str,
        status: str,
    ) -> tuple[int, str]:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO inbound_events (
                source, event_name, entity_id, project_id, delivery_id, payload_hash,
                raw_body, headers_json, received_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                event_name,
                entity_id,
                project_id,
                delivery_id,
                payload_digest,
                raw_body,
                headers_json,
                _utc_now(),
                status,
            ),
        )
        if cursor.rowcount == 1:
            return int(cursor.lastrowid), "ok"
        row_id = self._inbound_row_id(
            conn,
            source=source,
            event_name=event_name,
            entity_id=entity_id,
            project_id=project_id,
            delivery_id=delivery_id,
            payload_digest=payload_digest,
        )
        if row_id is None:
            raise sqlite3.IntegrityError("inbound event canonical row not found")
        return row_id, "already_recorded"

    def _insert_pending_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        inbound_event_id: int,
        kind: str,
        subscription: str | None,
        next_attempt_at: str,
        now: str,
    ) -> tuple[int, str]:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO pending_deliveries (
                inbound_event_id, kind, subscription, state, attempt_count,
                next_attempt_at, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', 0, ?, NULL, ?, ?)
            """,
            (inbound_event_id, kind, subscription, next_attempt_at, now, now),
        )
        if cursor.rowcount == 1:
            return int(cursor.lastrowid), "ok"
        row_id = self._pending_delivery_row_id(
            conn,
            inbound_event_id=inbound_event_id,
            kind=kind,
            subscription=subscription,
        )
        if row_id is None:
            raise sqlite3.IntegrityError("pending delivery canonical row not found")
        return row_id, "already_pending"

    def _pending_delivery_row_id(
        self,
        conn: sqlite3.Connection,
        *,
        inbound_event_id: int,
        kind: str,
        subscription: str | None,
    ) -> int | None:
        if subscription is not None:
            row = conn.execute(
                """
                SELECT id FROM pending_deliveries
                WHERE inbound_event_id = ? AND kind = ? AND subscription = ?
                """,
                (inbound_event_id, kind, subscription),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id FROM pending_deliveries
                WHERE inbound_event_id = ? AND kind = 'routing_resolution'
                    AND subscription IS NULL
                """,
                (inbound_event_id,),
            ).fetchone()
        return int(row[0]) if row is not None else None

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
