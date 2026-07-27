"""Shared builders for the dispatch tests.

Nothing here touches Postgres or the network: the optimiser is a pure function of hourly inputs,
a battery, and a tariff, which is exactly what makes it property-testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from energy_platform.config import BatteryConfig
from energy_platform.dispatch.model import HourInputs
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
