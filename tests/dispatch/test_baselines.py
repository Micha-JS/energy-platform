"""The three baselines, and the one place they are allowed to differ.

``naive_continuous`` and ``naive_telemetered`` run the *same policy* on the *same physics* -- both
go through M3's ``dispatch_hour``. The only difference is the boundary condition: the generator
restarts each Berlin day from a fixed initial state of charge, because a day's telemetry has to be
reproducible from that partition date alone, while the optimiser's baseline carries state across
the whole window. These tests pin that as the *only* difference, which is what makes the headline
comparison a statement about dispatch rather than about M3's partition purity.

Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import math

import pytest

from energy_platform.config import BatteryConfig
from energy_platform.connectors.synthetic import dispatch_hour
from energy_platform.dispatch import baselines
from energy_platform.dispatch.model import HourInputs, Scenario
from energy_platform.dispatch.pricing import WindowPrices, terminal_value_eur_kwh, window_prices
from energy_platform.tariffs.counterfactual import no_battery_flows
from tests.dispatch.conftest import BARE_DYNAMIC, FEED_IN, LOSSLESS, make_hours

# A day of surplus then a day of deficit: the battery fills on day one and, if state of charge
# survives midnight, empties on day two. That is exactly the energy M3 throws away.
SURPLUS_THEN_DEFICIT_PV = [4.0] * 6 + [0.0] * 6
SURPLUS_THEN_DEFICIT_LOAD = [0.0] * 6 + [2.0] * 6
FLAT_PRICE = [100.0] * 12


def _prices(
    pv: list[float], load: list[float], spot: list[float]
) -> tuple[WindowPrices, tuple[HourInputs, ...]]:
    prices: list[float | None] = list(spot)
    hours = make_hours(prices, pv, load)
    return window_prices(BARE_DYNAMIC, FEED_IN, hours), hours


def test_the_naive_baseline_calls_the_generators_own_dispatch_function() -> None:
    """Reuse, not re-implementation: the baseline's flows are ``dispatch_hour``'s, hour by hour.

    Recomputed here by calling the generator directly, so this fails if the baseline ever grows a
    second opinion about the naive policy.
    """
    prices, hours = _prices(SURPLUS_THEN_DEFICIT_PV, SURPLUS_THEN_DEFICIT_LOAD, FLAT_PRICE)
    result = baselines.naive_continuous(hours, prices, LOSSLESS, 0.0)

    soc = baselines.initial_soc_kwh(LOSSLESS)
    for index, hour in enumerate(result.hours):
        expected = dispatch_hour(
            SURPLUS_THEN_DEFICIT_PV[index], SURPLUS_THEN_DEFICIT_LOAD[index], soc, LOSSLESS
        )
        soc = expected.soc_kwh
        assert hour.battery_charge_kwh == pytest.approx(expected.charge)
        assert hour.battery_discharge_kwh == pytest.approx(expected.discharge)
        assert hour.grid_import_kwh == pytest.approx(expected.grid_import)
        assert hour.grid_export_kwh == pytest.approx(expected.grid_export)
        assert hour.soc_kwh == pytest.approx(expected.soc_kwh)


def test_carrying_state_of_charge_is_the_only_difference_from_the_telemetered_series() -> None:
    """Within one day the two naive baselines agree; across midnight they diverge, and by how much.

    The divergence *is* ``battery_unreturned_kwh``: energy the generator bought and discarded at the
    day boundary. Headlining an optimum against the telemetered series would measure that, not
    optimisation -- which is why the mart carries both.
    """
    prices, hours = _prices(SURPLUS_THEN_DEFICIT_PV, SURPLUS_THEN_DEFICIT_LOAD, FLAT_PRICE)
    continuous = baselines.naive_continuous(hours, prices, LOSSLESS, 0.0)

    # The generator's day-one behaviour, replayed: it starts every day from soc_min, so its first
    # six hours are identical to the continuous baseline's.
    soc = baselines.initial_soc_kwh(LOSSLESS)
    for index in range(6):
        expected = dispatch_hour(
            SURPLUS_THEN_DEFICIT_PV[index], SURPLUS_THEN_DEFICIT_LOAD[index], soc, LOSSLESS
        )
        soc = expected.soc_kwh
        assert continuous.hours[index].battery_charge_kwh == pytest.approx(expected.charge)

    # Day two: the generator would restart from soc_min and have nothing to give, so it would
    # import the whole load. The continuous baseline still holds day one's charge and serves it.
    restarted = baselines.initial_soc_kwh(LOSSLESS)
    discarded = soc - restarted
    assert discarded > 0.0, "the fixture must leave energy in the battery at the day boundary"

    served = sum(hour.battery_discharge_kwh or 0.0 for hour in continuous.hours[6:])
    assert served == pytest.approx(discarded, abs=1e-9), (
        "the continuous baseline should serve exactly the energy the generator would have thrown "
        "away at midnight"
    )


def test_the_no_battery_baseline_matches_the_m5_counterfactual_definition() -> None:
    """The same ``max(load - pv, 0)`` the economics marts use -- reused, so they cannot disagree."""
    prices, hours = _prices(SURPLUS_THEN_DEFICIT_PV, SURPLUS_THEN_DEFICIT_LOAD, FLAT_PRICE)
    result = baselines.no_battery(hours, prices, LOSSLESS, 0.0)

    for index, hour in enumerate(result.hours):
        expected = no_battery_flows(
            SURPLUS_THEN_DEFICIT_PV[index], SURPLUS_THEN_DEFICIT_LOAD[index]
        )
        assert expected is not None
        assert hour.grid_import_kwh == pytest.approx(expected.grid_import_kwh)
        assert hour.grid_export_kwh == pytest.approx(expected.grid_export_kwh)
        assert hour.battery_charge_kwh == 0.0
        assert hour.battery_discharge_kwh == 0.0
        assert hour.soc_kwh is None, "a scenario with no battery has no state of charge"


def test_the_no_battery_baseline_is_never_credited_for_terminal_energy() -> None:
    """It ends with exactly what it started with, so the terminal adjustment cannot flatter it."""
    prices, hours = _prices(SURPLUS_THEN_DEFICIT_PV, SURPLUS_THEN_DEFICIT_LOAD, FLAT_PRICE)
    result = baselines.no_battery(hours, prices, LOSSLESS, 0.25)

    assert result.soc_end_kwh == result.soc_start_kwh
    assert result.terminal_value_eur == 0.0
    assert result.objective_eur == result.net_cost_eur


def test_every_baseline_reports_the_scenario_it_actually_is() -> None:
    """Guards against a copy-paste that would silently label one dispatch as another."""
    prices, hours = _prices(SURPLUS_THEN_DEFICIT_PV, SURPLUS_THEN_DEFICIT_LOAD, FLAT_PRICE)
    battery = BatteryConfig()
    assert baselines.no_battery(hours, prices, battery, 0.0).scenario is Scenario.NO_BATTERY
    assert (
        baselines.naive_continuous(hours, prices, battery, 0.0).scenario
        is Scenario.NAIVE_CONTINUOUS
    )
    assert (
        baselines.naive_telemetered(hours, prices, battery, 0.0).scenario
        is Scenario.NAIVE_TELEMETERED
    )


def test_a_terminal_valuation_credits_stored_energy_at_what_it_can_deliver() -> None:
    """Credited at ``lambda * sqrt(rte)``, because that is what a stored kWh reaches the node as."""
    battery = BatteryConfig(
        capacity_kwh=10.0,
        soc_min=0.0,
        soc_max=1.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency=0.64,  # sqrt = 0.8 exactly, so the expected value is legible
    )
    prices, hours = _prices([4.0] * 4, [0.0] * 4, FLAT_PRICE[:4])
    result = baselines.naive_continuous(hours, prices, battery, 0.25)

    stored = result.soc_end_kwh - result.soc_start_kwh
    assert stored > 0.0
    assert result.terminal_value_eur == pytest.approx(0.25 * math.sqrt(0.64) * stored, abs=1e-6)


def test_the_declared_terminal_rate_is_the_cheapest_hour_not_the_average() -> None:
    """The choice that stops the optimiser hoarding to be credited for a full battery."""
    prices, _ = _prices([0.0] * 4, [1.0] * 4, [50.0, 100.0, 200.0, 400.0])
    # Bare dynamic tariff, no VAT: 50 EUR/MWh is 5 ct/kWh is 0.05 EUR/kWh.
    assert terminal_value_eur_kwh(prices) == pytest.approx(0.05)


def test_a_negative_retail_hour_leaves_terminal_energy_uncredited() -> None:
    """Conservative by construction: no positive lower bound exists, so none is claimed."""
    prices, _ = _prices([0.0] * 4, [1.0] * 4, [-400.0, 100.0, 200.0, 400.0])
    assert terminal_value_eur_kwh(prices) == 0.0
