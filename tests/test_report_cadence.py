"""Unit tests for the adaptive report-cadence formula."""

from __future__ import annotations

import math

import report_cadence


def test_at_target_healthy_trend_and_activity_gives_min_frequency() -> None:
    signals = report_cadence.compute_interval_hours(
        mrr_current=1000.0, mrr_projected=1000.0, events_24h=40
    )
    assert signals.pressure == 0.0
    assert signals.interval_hours == report_cadence.T_MAX_HOURS


def test_zero_revenue_flat_trend_and_no_activity_gives_max_frequency() -> None:
    signals = report_cadence.compute_interval_hours(
        mrr_current=0.0, mrr_projected=0.0, events_24h=0
    )
    assert signals.pressure == 1.0
    assert signals.interval_hours == report_cadence.T_MIN_HOURS


def test_interval_is_bounded_even_with_out_of_range_inputs() -> None:
    above_target = report_cadence.compute_interval_hours(
        mrr_current=5000.0, mrr_projected=5000.0, events_24h=1000
    )
    negative_ish = report_cadence.compute_interval_hours(
        mrr_current=-10.0, mrr_projected=-10.0, events_24h=-5
    )
    assert report_cadence.T_MIN_HOURS <= above_target.interval_hours <= report_cadence.T_MAX_HOURS
    assert report_cadence.T_MIN_HOURS <= negative_ish.interval_hours <= report_cadence.T_MAX_HOURS
    assert above_target.gap == 0.0
    assert negative_ish.gap == 1.0


def test_interval_is_monotonic_in_mrr_current() -> None:
    lower = report_cadence.compute_interval_hours(mrr_current=0.0, mrr_projected=500.0, events_24h=20)
    higher = report_cadence.compute_interval_hours(mrr_current=500.0, mrr_projected=500.0, events_24h=20)
    assert higher.interval_hours > lower.interval_hours


def test_interval_is_monotonic_in_events_24h() -> None:
    quiet = report_cadence.compute_interval_hours(mrr_current=200.0, mrr_projected=200.0, events_24h=0)
    busy = report_cadence.compute_interval_hours(mrr_current=200.0, mrr_projected=200.0, events_24h=40)
    assert busy.interval_hours > quiet.interval_hours


def test_midpoint_pressure_is_the_geometric_midpoint_of_the_bounds() -> None:
    signals = report_cadence.compute_interval_hours(
        mrr_current=500.0, mrr_projected=500.0, events_24h=20
    )
    assert signals.pressure == 0.5
    expected = math.sqrt(report_cadence.T_MIN_HOURS * report_cadence.T_MAX_HOURS)
    assert math.isclose(signals.interval_hours, expected, rel_tol=1e-9)


def test_custom_params_change_the_computed_interval() -> None:
    params = report_cadence.CadenceParams(
        mrr_target_eur=100.0, t_min_hours=2.0, t_max_hours=24.0
    )
    signals = report_cadence.compute_interval_hours(
        mrr_current=100.0, mrr_projected=100.0, events_24h=40, params=params
    )
    assert signals.gap == 0.0
    assert signals.interval_hours == 24.0


def test_speed_multiplier_defaults_to_one_and_leaves_interval_unchanged() -> None:
    baseline = report_cadence.compute_interval_hours(
        mrr_current=500.0, mrr_projected=500.0, events_24h=20
    )
    with_default_speed = report_cadence.compute_interval_hours(
        mrr_current=500.0,
        mrr_projected=500.0,
        events_24h=20,
        params=report_cadence.CadenceParams(speed_multiplier=1.0),
    )
    assert with_default_speed.interval_hours == baseline.interval_hours


def test_speed_multiplier_scales_interval_and_can_exceed_bounds() -> None:
    baseline = report_cadence.compute_interval_hours(
        mrr_current=1000.0, mrr_projected=1000.0, events_24h=40
    )
    faster = report_cadence.compute_interval_hours(
        mrr_current=1000.0,
        mrr_projected=1000.0,
        events_24h=40,
        params=report_cadence.CadenceParams(speed_multiplier=2.0),
    )
    slower = report_cadence.compute_interval_hours(
        mrr_current=1000.0,
        mrr_projected=1000.0,
        events_24h=40,
        params=report_cadence.CadenceParams(speed_multiplier=0.5),
    )
    assert faster.interval_hours == baseline.interval_hours / 2.0
    assert slower.interval_hours == baseline.interval_hours / 0.5
    # baseline is already pinned at T_MAX_HOURS (pressure=0) — the manual
    # override deliberately pushes past the formula's own upper bound.
    assert slower.interval_hours > report_cadence.T_MAX_HOURS


def test_validate_cadence_params_rejects_out_of_range_speed_multiplier() -> None:
    assert "speed_multiplier" in report_cadence.validate_cadence_params(
        report_cadence.CadenceParams(speed_multiplier=0.0)
    )
    assert "speed_multiplier" in report_cadence.validate_cadence_params(
        report_cadence.CadenceParams(speed_multiplier=100.0)
    )


def test_cadence_params_round_trip_through_dict() -> None:
    params = report_cadence.CadenceParams(mrr_target_eur=250.0, t_max_hours=100.0)
    restored = report_cadence.cadence_params_from_dict(report_cadence.cadence_params_to_dict(params))
    assert restored.mrr_target_eur == 250.0
    assert restored.t_max_hours == 100.0
    assert restored.legacy_revenue_cutover == params.legacy_revenue_cutover


def test_cadence_params_from_dict_falls_back_to_defaults_on_missing_or_bad_fields() -> None:
    params = report_cadence.cadence_params_from_dict(
        {"mrr_target_eur": "not-a-number", "weight_gap": 0.9, "legacy_revenue_cutover": "not-a-date"}
    )
    defaults = report_cadence.CadenceParams()
    assert params.mrr_target_eur == defaults.mrr_target_eur
    assert params.weight_gap == 0.9
    assert params.legacy_revenue_cutover == defaults.legacy_revenue_cutover


def test_validate_cadence_params_rejects_bad_bounds_and_weights() -> None:
    assert report_cadence.validate_cadence_params(report_cadence.CadenceParams()) == ""
    assert "mrr_target_eur" in report_cadence.validate_cadence_params(
        report_cadence.CadenceParams(mrr_target_eur=0)
    )
    assert "events_baseline_24h" in report_cadence.validate_cadence_params(
        report_cadence.CadenceParams(events_baseline_24h=-1)
    )
    assert "weight_gap" in report_cadence.validate_cadence_params(
        report_cadence.CadenceParams(weight_gap=-0.1)
    )
    assert "t_min_hours" in report_cadence.validate_cadence_params(
        report_cadence.CadenceParams(t_min_hours=0)
    )
    assert "t_max_hours" in report_cadence.validate_cadence_params(
        report_cadence.CadenceParams(t_min_hours=10, t_max_hours=5)
    )


def test_parse_cadence_overrides_accepts_a_partial_valid_body() -> None:
    overrides, error = report_cadence.parse_cadence_overrides(
        {"mrr_target_eur": 500, "legacy_revenue_cutover": "2026-01-01"}
    )
    assert error == ""
    assert overrides == {"mrr_target_eur": 500.0, "legacy_revenue_cutover": "2026-01-01"}


def test_parse_cadence_overrides_rejects_unknown_field() -> None:
    overrides, error = report_cadence.parse_cadence_overrides({"bogus_field": 1})
    assert overrides == {}
    assert "bogus_field" in error


def test_parse_cadence_overrides_rejects_non_numeric_value() -> None:
    overrides, error = report_cadence.parse_cadence_overrides({"mrr_target_eur": "not-a-number"})
    assert overrides == {}
    assert "mrr_target_eur" in error


def test_parse_cadence_overrides_rejects_bool_as_numeric() -> None:
    overrides, error = report_cadence.parse_cadence_overrides({"weight_gap": True})
    assert overrides == {}
    assert "weight_gap" in error


def test_parse_cadence_overrides_rejects_malformed_date() -> None:
    overrides, error = report_cadence.parse_cadence_overrides({"legacy_revenue_cutover": "not-a-date"})
    assert overrides == {}
    assert "legacy_revenue_cutover" in error
