"""Unit tests for report-cadence poller schedule state."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
import report_cadence
from conftest import TodoistProxyFixture


def _module():
    return importlib.reload(importlib.import_module("report_cadence_poller"))


def test_status_round_trips_through_existing_meta_table(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    poller = _module()
    evaluated_at = datetime(2026, 7, 11, 10, 30, tzinfo=timezone.utc)
    last_fired_at = datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)
    params = report_cadence.CadenceParams(t_min_hours=2, t_max_hours=4)
    signals = report_cadence.compute_interval_hours(
        mrr_current=0,
        mrr_projected=0,
        events_24h=0,
        params=params,
    )
    status = poller._build_status(
        status="scheduled",
        evaluated_at=evaluated_at,
        last_fired_at=last_fired_at,
        signals=signals,
        params=params,
    )

    conn = poller._connect_db()
    try:
        poller._record_status(conn, status)
        restored = poller._get_status(conn)
    finally:
        conn.close()

    assert restored == status
    assert restored["initialized"] is True
    assert restored["next_fire_at"] == "2026-07-11T11:00:00+00:00"
    assert restored["signals"]["interval_hours"] == 2.0
    assert todoist_proxy_fixture.report_cadence_db.exists()


def test_status_without_last_fire_is_not_initialized(
    todoist_proxy_fixture: TodoistProxyFixture,
) -> None:
    poller = _module()
    signals = report_cadence.compute_interval_hours(
        mrr_current=1000,
        mrr_projected=1000,
        events_24h=40,
    )

    status = poller._build_status(
        status="not_initialized",
        evaluated_at=datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc),
        last_fired_at=None,
        signals=signals,
        params=report_cadence.CadenceParams(),
    )

    assert status["initialized"] is False
    assert status["next_fire_at"] is None
    assert status["due"] is None


def test_dry_run_does_not_write_status_snapshot(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poller = _module()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setattr(sys, "argv", ["report_cadence_poller.py", "--dry-run"])
    monkeypatch.setattr(report_cadence, "fetch_mrr_signals", lambda *_args, **_kwargs: (0.0, 0.0))

    assert poller.main() == 0

    conn = poller._connect_db()
    try:
        assert poller._get_last_fired_at(conn) is None
        assert poller._get_status(conn) is None
    finally:
        conn.close()


def test_spark_disabled_due_run_does_not_deliver_or_advance_last_fire(
    todoist_proxy_fixture: TodoistProxyFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poller = _module()
    original_last_fire = datetime.now(timezone.utc) - timedelta(hours=3)
    todoist_proxy_fixture.control_config_file.write_text(
        json.dumps({"global": {"spark_enabled": False}}) + "\n"
    )
    conn = poller._connect_db()
    try:
        poller._record_fired_at(conn, original_last_fire)
    finally:
        conn.close()

    delivered: list[tuple[str, str, dict]] = []
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setattr(sys, "argv", ["report_cadence_poller.py"])
    monkeypatch.setattr(report_cadence, "fetch_mrr_signals", lambda *_args, **_kwargs: (0.0, 0.0))
    monkeypatch.setattr(report_cadence, "compose_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(
        poller,
        "_load_routing",
        lambda: (
            {
                poller.LOWKEYCODES_PROJECT_ID: {
                    "max-lowkeycodes": {
                        "agent": "max",
                        "responsible_uids": [poller.MAX_UID],
                    }
                }
            },
            {"max-lowkeycodes": "http://127.0.0.1:8644"},
        ),
    )
    monkeypatch.setattr(
        poller,
        "_deliver",
        lambda upstream, subscription, event: delivered.append((upstream, subscription, event)) or True,
    )

    assert poller.main() == 0

    conn = poller._connect_db()
    try:
        assert poller._get_last_fired_at(conn) == original_last_fire
        status = poller._get_status(conn)
    finally:
        conn.close()
    assert delivered == []
    assert status is not None
    assert status["status"] == "suppressed"
    assert status["last_fired_at"] == original_last_fire.isoformat()
