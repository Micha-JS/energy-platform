"""Solving one coverage window: read the warehouse, run four scenarios, hand back results.

This is the only place that decides *which* scenarios exist and *what* they share. All four are
solved from the same hourly inputs, priced by the same tariff engine, started from the same state
of charge and settled with the same terminal valuation -- so any difference between their totals is
a difference in dispatch and nothing else. Getting that sharing wrong is the easiest way to produce
a savings number that measures the accounting rather than the battery, which is why it lives in one
short function instead of being spread across four call sites.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from energy_platform.config import BatteryConfig
from energy_platform.dispatch import baselines
from energy_platform.dispatch.model import DispatchResult, HourInputs
from energy_platform.dispatch.optimizer import solve_window
from energy_platform.dispatch.pricing import (
    CONTINUATION_VALUE_ENV,
    TERMINAL_VALUE_ENV,
    terminal_value_eur_kwh,
    window_prices,
)
from energy_platform.dispatch.windows import CoverageWindow
from energy_platform.tariffs.catalog import TariffSpec


@dataclass(frozen=True, slots=True)
class WindowSolution:
    """Every scenario for one (window, site, tariff), plus what they were all solved under."""

    window: CoverageWindow
    region: str
    tariff_id: str
    terminal_value_eur_kwh: float
    hours: tuple[HourInputs, ...]
    results: tuple[DispatchResult, ...]

    @property
    def expected_hours(self) -> int:
        return self.window.expected_hours


def terminal_value_override() -> float | None:
    """Read ``ENERGY_DISPATCH_TERMINAL_VALUE_CT_KWH``. ``None`` when unset, as it normally is."""
    return _numeric_env(TERMINAL_VALUE_ENV)


def continuation_value_override() -> float | None:
    """Read ``ENERGY_DISPATCH_CONTINUATION_CT_KWH``. ``None`` when unset, as it normally is.

    M8's planner-side twin of the above, and a separate knob because it is a separate quantity: this
    one shapes what each daily plan *decides*, the other settles every scenario. The sweep reported
    in :func:`energy_platform.dispatch.pricing.planning_continuation_eur_kwh` is reproducible from
    the command line through this variable.
    """
    return _numeric_env(CONTINUATION_VALUE_ENV)


def _numeric_env(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"expected a number for {name}, got {raw!r}") from exc


def solve(
    window: CoverageWindow,
    region: str,
    hours: Sequence[HourInputs],
    spec: TariffSpec,
    feed_in: TariffSpec,
    battery: BatteryConfig,
) -> WindowSolution:
    """Run all four scenarios over one window under one consumption tariff.

    The terminal valuation is derived **once**, from this tariff's own priced hours, and passed to
    every scenario. A per-scenario valuation would let the optimum be credited for stored energy at
    a different rate than the baseline it is compared against, which would make the savings figure
    partly an artefact of the adjustment.
    """
    prices = window_prices(spec, feed_in, hours)
    terminal = terminal_value_eur_kwh(prices, terminal_value_override())

    results = (
        baselines.no_battery(hours, prices, battery, terminal),
        baselines.naive_telemetered(hours, prices, battery, terminal),
        baselines.naive_continuous(hours, prices, battery, terminal),
        solve_window(hours, prices, battery, terminal),
    )
    return WindowSolution(
        window=window,
        region=region,
        tariff_id=spec.tariff_id,
        terminal_value_eur_kwh=terminal,
        hours=tuple(hours),
        results=results,
    )
