"""Adaptive report-cadence formula and prompt composition for Max's business-state trigger.

The interval Max gets re-woken on scales between MRR_TARGET-aware urgency
and observed company activity: further behind target, and/or more stalled
(few recent ledger events), means a shorter interval; on/above target with
healthy activity means a longer one. See ``compute_interval_hours`` for the
exact formula.

All tunables (target, weights, bounds, the legacy-revenue cutover date) live
in ``CadenceParams``. Defaults come from this module's constants; callers
that want the live-editable values from the control UI (``todoist-control.json``'s
``report_cadence`` key) build a ``CadenceParams`` via ``cadence_params_from_dict``
— this module itself never touches that file, keeping it config-file-agnostic.

Kept separate from ``report_cadence_poller.py`` (which owns state/HTTP
delivery) so the formula, Stripe aggregation, and prompt composition are
unit-testable without mocking network calls or the delivery pipeline.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

STRIPE_API_BASE = "https://api.stripe.com/v1"
REQUEST_TIMEOUT = 15

MRR_TARGET_EUR = 1000.0
EVENTS_BASELINE_24H = 40.0
WEIGHT_GAP = 0.45
WEIGHT_SHORTFALL = 0.35
WEIGHT_STAGNATION = 0.20
T_MIN_HOURS = 1.0
T_MAX_HOURS = 168.0

# Manual global override (control-UI speed slider): divides the formula's
# interval_hours result. 1.0 = automatic (formula decides, unchanged).
SPEED_MULTIPLIER_DEFAULT = 1.0
SPEED_MULTIPLIER_MIN = 0.1
SPEED_MULTIPLIER_MAX = 10.0

# 2026-06-10: confirmed cutover in lowkeycodes/finance/scoreboard.md — charges
# before this date are a previous business's revenue, not LowKeyCodes's.
LEGACY_REVENUE_CUTOVER = datetime(2026, 6, 10, tzinfo=timezone.utc)

PROMPT_PATH = Path(
    "/home/filipp/Repos/HQ1/Obsidian/HQ1/lowkeycodes/Filipps control prompt/"
    "Report current LowKeyCodes business state to Filipp.md"
)

CADENCE_CONFIG_KEY = "report_cadence"
CADENCE_NUMERIC_FIELDS = (
    "mrr_target_eur",
    "events_baseline_24h",
    "weight_gap",
    "weight_shortfall",
    "weight_stagnation",
    "t_min_hours",
    "t_max_hours",
    "speed_multiplier",
)
CADENCE_DATE_FIELD = "legacy_revenue_cutover"


@dataclass(frozen=True)
class CadenceParams:
    """Tunable inputs to the adaptive-cadence formula, editable from the control UI."""

    mrr_target_eur: float = MRR_TARGET_EUR
    events_baseline_24h: float = EVENTS_BASELINE_24H
    weight_gap: float = WEIGHT_GAP
    weight_shortfall: float = WEIGHT_SHORTFALL
    weight_stagnation: float = WEIGHT_STAGNATION
    t_min_hours: float = T_MIN_HOURS
    t_max_hours: float = T_MAX_HOURS
    speed_multiplier: float = SPEED_MULTIPLIER_DEFAULT
    legacy_revenue_cutover: datetime = LEGACY_REVENUE_CUTOVER


@dataclass(frozen=True)
class CadenceSignals:
    """Computed inputs and outputs of one cadence evaluation."""

    mrr_current: float
    mrr_projected: float
    events_24h: int
    gap: float
    shortfall: float
    stagnation: float
    pressure: float
    interval_hours: float


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def compute_interval_hours(
    mrr_current: float,
    mrr_projected: float,
    events_24h: int,
    *,
    params: CadenceParams = CadenceParams(),
) -> CadenceSignals:
    """Map (current MRR, projected MRR, trailing-24h event count) to a
    report interval in hours, bounded to [params.t_min_hours, params.t_max_hours].

    Three 0-1 pressure sub-scores (1 = needs max attention):
      gap        = how far below target MRR is today
      shortfall  = how far below target the 30d-forward projection is
      stagnation = how quiet the ledger has been in the last 24h

    P = weighted sum of the three, clamped to [0, 1]. T_hours = t_max *
    (t_min/t_max) ** P is an exponential interpolation between the two
    bounds (linear would collapse most of a wide range near one extreme),
    so P=0 -> t_max, P=1 -> t_min. That result is bounded to
    [params.t_min_hours, params.t_max_hours].

    ``params.speed_multiplier`` then divides that bounded result as a manual
    global override (the control-UI speed slider) — >1 speeds delivery up,
    <1 slows it down, independent of the automatic pressure calculation.
    This step is deliberately *not* reclamped to [t_min_hours, t_max_hours];
    its entire purpose is letting an operator push outside those bounds on
    demand. 1.0 (the default) leaves the formula's result untouched.
    """

    gap = clamp(1 - mrr_current / params.mrr_target_eur)
    shortfall = clamp(1 - mrr_projected / params.mrr_target_eur)
    stagnation = clamp(1 - events_24h / params.events_baseline_24h)

    pressure = clamp(
        params.weight_gap * gap
        + params.weight_shortfall * shortfall
        + params.weight_stagnation * stagnation
    )
    interval_hours = params.t_max_hours * (params.t_min_hours / params.t_max_hours) ** pressure
    interval_hours = interval_hours / params.speed_multiplier

    return CadenceSignals(
        mrr_current=mrr_current,
        mrr_projected=mrr_projected,
        events_24h=events_24h,
        gap=gap,
        shortfall=shortfall,
        stagnation=stagnation,
        pressure=pressure,
        interval_hours=interval_hours,
    )


def cadence_params_to_dict(params: CadenceParams) -> dict[str, Any]:
    data: dict[str, Any] = {name: getattr(params, name) for name in CADENCE_NUMERIC_FIELDS}
    data[CADENCE_DATE_FIELD] = params.legacy_revenue_cutover.date().isoformat()
    return data


def cadence_params_from_dict(data: Mapping[str, Any]) -> CadenceParams:
    """Merge a (possibly partial/malformed) override dict over the defaults.

    Lenient by design — used to build the *effective* params for a run, so a
    single bad/missing field falls back to that field's default rather than
    failing the whole load. Use ``parse_cadence_overrides`` to strictly
    validate user input before it's persisted.
    """

    defaults = CadenceParams()
    values: dict[str, Any] = {}
    for name in CADENCE_NUMERIC_FIELDS:
        raw = data.get(name, getattr(defaults, name))
        try:
            values[name] = float(raw)
        except (TypeError, ValueError):
            values[name] = getattr(defaults, name)

    cutover = defaults.legacy_revenue_cutover
    raw_cutover = data.get(CADENCE_DATE_FIELD)
    if isinstance(raw_cutover, str) and raw_cutover:
        try:
            parsed = datetime.fromisoformat(raw_cutover)
            cutover = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    values[CADENCE_DATE_FIELD] = cutover

    return CadenceParams(**values)


def validate_cadence_params(params: CadenceParams) -> str:
    """Return an error message, or '' if params are internally usable."""

    if params.mrr_target_eur <= 0:
        return "mrr_target_eur must be > 0"
    if params.events_baseline_24h <= 0:
        return "events_baseline_24h must be > 0"
    for name in ("weight_gap", "weight_shortfall", "weight_stagnation"):
        if getattr(params, name) < 0:
            return f"{name} must be >= 0"
    if params.t_min_hours <= 0:
        return "t_min_hours must be > 0"
    if params.t_max_hours < params.t_min_hours:
        return "t_max_hours must be >= t_min_hours"
    if not (SPEED_MULTIPLIER_MIN <= params.speed_multiplier <= SPEED_MULTIPLIER_MAX):
        return f"speed_multiplier must be between {SPEED_MULTIPLIER_MIN} and {SPEED_MULTIPLIER_MAX}"
    return ""


def parse_cadence_overrides(body: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Strictly validate a partial ``CadenceParams`` override dict from an API request.

    Returns ``(normalized_overrides, "")`` on success, or ``({}, error)`` on
    the first invalid field. Unlike ``cadence_params_from_dict`` this never
    silently substitutes a default for a bad value — the caller should
    reject the request instead.
    """

    known = set(CADENCE_NUMERIC_FIELDS) | {CADENCE_DATE_FIELD}
    unknown = set(body) - known
    if unknown:
        return {}, f"unsupported field(s): {', '.join(sorted(unknown))}"

    normalized: dict[str, Any] = {}
    for name in CADENCE_NUMERIC_FIELDS:
        if name not in body:
            continue
        value = body[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return {}, f"{name} must be a number"
        normalized[name] = float(value)

    if CADENCE_DATE_FIELD in body:
        raw = body[CADENCE_DATE_FIELD]
        if not isinstance(raw, str):
            return {}, f"{CADENCE_DATE_FIELD} must be an ISO date string (YYYY-MM-DD)"
        try:
            datetime.fromisoformat(raw)
        except ValueError:
            return {}, f"{CADENCE_DATE_FIELD} must be an ISO date string (YYYY-MM-DD)"
        normalized[CADENCE_DATE_FIELD] = raw

    return normalized, ""


def _stripe_get(path: str, api_key: str, params: dict[str, str]) -> dict[str, Any]:
    url = f"{STRIPE_API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read())


def _sum_charges_eur(
    api_key: str, *, since: datetime, until: datetime, legacy_cutover: datetime
) -> float:
    """Sum succeeded, non-refunded EUR Stripe charges in [since, until),
    excluding pre-cutover legacy revenue (see ``legacy_cutover``).

    This is a proxy-MRR: LowKeyCodes has no live Stripe Subscription
    objects yet, so "MRR" is approximated as the trailing/leading 30-day
    sum of qualifying one-off charges.
    """

    total_cents = 0
    params = {
        "created[gte]": str(int(since.timestamp())),
        "created[lt]": str(int(until.timestamp())),
        "limit": "100",
    }
    starting_after: str | None = None
    while True:
        query = dict(params)
        if starting_after:
            query["starting_after"] = starting_after
        page = _stripe_get("charges", api_key, query)
        for charge in page.get("data", []):
            if charge.get("status") != "succeeded" or not charge.get("paid"):
                continue
            if charge.get("currency", "").lower() != "eur":
                continue
            created_at = datetime.fromtimestamp(charge["created"], tz=timezone.utc)
            if created_at < legacy_cutover:
                continue
            total_cents += charge.get("amount", 0) - charge.get("amount_refunded", 0)
        if not page.get("has_more"):
            break
        starting_after = page["data"][-1]["id"]
    return total_cents / 100.0


def fetch_mrr_signals(
    api_key: str, *, now: datetime | None = None, params: CadenceParams = CadenceParams()
) -> tuple[float, float]:
    """Return (mrr_current, mrr_projected).

    mrr_current is the trailing-30-day qualifying charge sum. mrr_projected
    repeats the last 30 days' growth for the next 30 (mrr_current + (mrr_current
    - mrr_30d_ago)), floored at 0.
    """

    now = now or datetime.now(timezone.utc)
    window = timedelta(days=30)
    mrr_current = _sum_charges_eur(
        api_key, since=now - window, until=now, legacy_cutover=params.legacy_revenue_cutover
    )
    mrr_prior = _sum_charges_eur(
        api_key,
        since=now - 2 * window,
        until=now - window,
        legacy_cutover=params.legacy_revenue_cutover,
    )
    mrr_projected = max(0.0, mrr_current + (mrr_current - mrr_prior))
    return mrr_current, mrr_projected


def compose_prompt(
    signals: CadenceSignals,
    *,
    params: CadenceParams = CadenceParams(),
    prompt_path: Path = PROMPT_PATH,
) -> str:
    """Read the Obsidian control prompt verbatim and append a generated
    data block so Max can see the numbers that triggered this occurrence."""

    base = prompt_path.read_text()
    data_block = (
        "\n\n---\n\n"
        "## Current adaptive-cadence signals (auto-generated, do not edit)\n\n"
        f"- MRR (trailing 30d, LowKeyCodes-attributable, Stripe): "
        f"EUR {signals.mrr_current:.2f} (target EUR {params.mrr_target_eur:.0f})\n"
        f"- Projected MRR (next 30d at current trend): EUR {signals.mrr_projected:.2f}\n"
        f"- Proxy events in the last 24h: {signals.events_24h}\n"
        f"- Pressure score: {signals.pressure:.2f} "
        f"(gap={signals.gap:.2f}, shortfall={signals.shortfall:.2f}, "
        f"stagnation={signals.stagnation:.2f})\n"
        f"- This occurrence's computed interval: {signals.interval_hours:.1f}h "
        f"(formula bounds {params.t_min_hours:g}h–{params.t_max_hours:g}h, "
        f"manual speed override {params.speed_multiplier:g}x)\n"
    )
    return base + data_block
