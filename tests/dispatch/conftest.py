"""Shared builders for the dispatch tests.

Nothing here touches Postgres or the network: the optimiser is a pure function of hourly inputs,
a battery, and a tariff, which is exactly what makes it property-testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest

from energy_platform.config import BatteryConfig
from energy_platform.dispatch.decision import decision_time_ms
from energy_platform.dispatch.model import HourInputs
from energy_platform.forecasting.runner import TARGET_PV
from energy_platform.forecasting.serving import (
    PLANNER_MODELS,
    DayForecast,
    HourForecast,
    ServingPlan,
)
from energy_platform.orchestration.ingest import BERLIN
from energy_platform.tariffs.catalog import TariffKind, TariffSpec

WINDOW_START = datetime(2024, 3, 28, tzinfo=UTC)

# A round battery, so hand-worked examples stay legible: 10 kWh usable from empty, 5 kW both ways,
# 100% round trip. Losses and reserve floors are varied by the property tests instead.
LOSSLESS = BatteryConfig(
    capacity_kwh=10.0,
    soc_min=0.0,
    soc_max=1.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    round_trip_efficiency=1.0,
)

# A dynamic tariff with no margin, no pass-through and no VAT, so the import price is exactly
# spot/10 ct/kWh. Keeps the arithmetic in an example test the thing under test.
BARE_DYNAMIC = TariffSpec(
    tariff_id="bare_dynamic",
    label="",
    kind=TariffKind.DYNAMIC,
    margin_ct_kwh=0.0,
    pass_through_ct_kwh=0.0,
    base_fee_eur_month=0.0,
    vat_rate=0.0,
)

FEED_IN = TariffSpec(tariff_id="bare_feed_in", label="", kind=TariffKind.FEED_IN, price_ct_kwh=8.11)

# No export compensation. Useful for isolating the *shifting* decision in an example: with exports
# worth nothing, the only reason to store a kWh is to serve a later hour's load, so the optimum is
# whatever hand arithmetic says it is rather than a mixture of shifting and selling.
ZERO_FEED_IN = TariffSpec(
    tariff_id="no_feed_in", label="", kind=TariffKind.FEED_IN, price_ct_kwh=0.0
)


def make_hours(
    prices_eur_mwh: Sequence[float | None],
    pv_kwh: Sequence[float | None] | None = None,
    load_kwh: Sequence[float | None] | None = None,
    *,
    start: datetime = WINDOW_START,
) -> tuple[HourInputs, ...]:
    """Build a window of inputs from parallel series, defaulting PV to nothing and load to 1 kWh.

    Telemetered flows default to the no-battery flows, so ``naive_telemetered`` is well defined
    without every test having to spell one out.
    """
    count = len(prices_eur_mwh)
    pv = list(pv_kwh) if pv_kwh is not None else [0.0] * count
    load = list(load_kwh) if load_kwh is not None else [1.0] * count
    hours = []
    for index in range(count):
        generation, consumption = pv[index], load[index]
        residual = None if generation is None or consumption is None else generation - consumption
        hours.append(
            HourInputs(
                ts_utc=start + timedelta(hours=index),
                pv_production_kwh=generation,
                household_load_kwh=consumption,
                price_eur_mwh=prices_eur_mwh[index],
                battery_charge_kwh=None if residual is None else 0.0,
                battery_discharge_kwh=None if residual is None else 0.0,
                grid_import_kwh=None if residual is None else max(-residual, 0.0),
                grid_export_kwh=None if residual is None else max(residual, 0.0),
                soc_frac=0.0,
            )
        )
    return tuple(hours)


@pytest.fixture
def lossless() -> BatteryConfig:
    return LOSSLESS


# -- M8: building serving plans without fitting anything ------------------------------------

# Berlin is UTC+2 in May, so Berlin midnight on the 1st is 22:00 UTC on 30 April. Simulated days are
# built from this instant so the day boundaries the simulation groups on are real Berlin midnights
# rather than UTC ones -- getting that wrong would silently shift every day by two hours and make
# the SoC chain meaningless.
MAY_FIRST_BERLIN = datetime(2024, 4, 30, 22, 0, tzinfo=UTC)


def berlin_days(count: int, *, start: date = date(2024, 5, 1)) -> list[date]:
    return [start + timedelta(days=offset) for offset in range(count)]


def day_profile(hour_of_day: int, day_index: int = 0) -> tuple[float, float, float]:
    """``(spot EUR/MWh, pv kWh, load kWh)`` for one local hour of a plausible May day.

    Shaped so the battery has somewhere to go: a modest PV bump around noon, an evening load peak,
    and an evening price spike -- so shifting is worth something and a forecast error is visible in
    money. A flat profile would make every scenario identical and every property test vacuous.

    ``day_index`` tilts each day slightly, and that is a **performance** requirement rather than
    realism for its own sake. With byte-identical days the MILP over a multi-day span has an
    enormous symmetric optimum -- every day's schedule is interchangeable with every other's -- and
    branch and bound explores it exhaustively: a three-day window took 64 seconds to solve, against
    under a second for the same span of real seeded data. Detuning the days by a few percent breaks
    the symmetry, and is closer to weather anyway.
    """
    tilt = 1.0 + 0.07 * day_index
    spot = (250.0 if 17 <= hour_of_day <= 20 else 20.0) * tilt
    pv = max(0.0, 1.2 * tilt - abs(hour_of_day - 13) * 0.25)
    load = 2.4 if 17 <= hour_of_day <= 20 else (0.9 if 6 <= hour_of_day <= 22 else 0.3)
    return spot, pv, load


def simulated_hours(days: Sequence[date]) -> tuple[HourInputs, ...]:
    """A contiguous run of 24-hour Berlin days, priced and with physics."""
    hours: list[HourInputs] = []
    for index in range(len(days) * 24):
        ts = MAY_FIRST_BERLIN + timedelta(hours=index)
        local = ts.astimezone(BERLIN)
        spot, pv, load = day_profile(local.hour, day_index=(local.date() - days[0]).days)
        hours.append(
            HourInputs(
                ts_utc=ts,
                pv_production_kwh=pv,
                household_load_kwh=load,
                price_eur_mwh=spot,
            )
        )
    return tuple(hours)


def serving_plan(
    target: str,
    hours: Sequence[HourInputs],
    days: Sequence[date],
    *,
    bias: float = 1.0,
    skip: Sequence[date] = (),
    vintage_issue_ms: int | None = None,
) -> ServingPlan:
    """A :class:`ServingPlan` built by hand, so no model has to be fitted to test the simulation.

    ``bias`` scales the truth: 1.0 is perfect foresight, 0.7 a systematic under-forecast. That is
    the knob the property tests turn -- what matters for the invariants is that the plan and the
    actuals *disagree*, not how a real model would have disagreed.
    """
    model_key = PLANNER_MODELS[target]
    by_date: dict[date, list[HourInputs]] = {}
    for hour in hours:
        by_date.setdefault(hour.ts_utc.astimezone(BERLIN).date(), []).append(hour)

    forecasts = []
    for day in days:
        if day in skip or day not in by_date:
            continue
        day_hours = by_date[day]
        forecasts.append(
            DayForecast(
                target=target,
                target_day=day,
                decision_ms=decision_time_ms(day),
                vintage_issue_ms=(
                    vintage_issue_ms
                    if vintage_issue_ms is not None
                    else decision_time_ms(day) - 6 * 3_600_000
                ),
                model_key=model_key,
                config_hash="0" * 64,
                fit_day=days[0],
                hours=tuple(
                    HourForecast(
                        int(hour.ts_utc.timestamp() * 1000),
                        _truth(hour, target) * bias,
                        was_clamped=False,
                    )
                    for hour in day_hours
                ),
            )
        )
    return ServingPlan(
        target=target,
        model_key=model_key,
        window_start=days[0],
        window_end=days[-1],
        days=tuple(forecasts),
        models=(),
        unserved_days=tuple(day for day in days if day in skip),
    )


def _truth(hour: HourInputs, target: str) -> float:
    value = hour.pv_production_kwh if target == TARGET_PV else hour.household_load_kwh
    assert value is not None  # the builders above never emit a gap
    return value
