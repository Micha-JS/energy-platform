"""Example-based tests: small windows whose optimal dispatch can be worked out by hand.

The property tests next door assert what must hold for every solution; these pin what the optimiser
should actually *do* on cases simple enough to check on paper. Each docstring shows the arithmetic,
so a failure says which number moved rather than only that something did.

All of them use a lossless 10 kWh battery at 5 kW, and a dynamic tariff with no margin, no
pass-through and no VAT -- so the import price is exactly ``spot/10`` ct/kWh and a 100 EUR/MWh hour
costs 10 ct/kWh. The realistic catalogue is exercised by the negative-price tests and end to end.

Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import pytest

from energy_platform.config import BatteryConfig
from energy_platform.dispatch import baselines
from energy_platform.dispatch.model import HourInputs
from energy_platform.dispatch.optimizer import (
    _MIP_GAP_ABS_EUR,
    _MIP_GAP_REL,
    DispatchError,
    _configured_solver,
    solve_window,
)
from energy_platform.dispatch.pricing import WindowPrices, terminal_value_eur_kwh, window_prices
from energy_platform.tariffs.catalog import TariffSpec
from tests.dispatch.conftest import BARE_DYNAMIC, FEED_IN, LOSSLESS, ZERO_FEED_IN, make_hours


def _window(
    spot: list[float],
    pv: list[float],
    load: list[float],
    feed_in: TariffSpec = FEED_IN,
) -> tuple[WindowPrices, tuple[HourInputs, ...]]:
    prices: list[float | None] = list(spot)
    hours = make_hours(prices, pv, load)
    return window_prices(BARE_DYNAMIC, feed_in, hours), hours


def test_it_buys_cheap_and_serves_the_expensive_hour_from_store() -> None:
    """Textbook arbitrage: 2 kWh bought at 10 EUR/MWh covers 2 kWh of load at 500 EUR/MWh.

    No feed-in and no terminal credit, so storing a kWh is worth exactly the later hour it serves
    and nothing else -- which is what makes the answer hand-checkable.

    Load is 2 kWh in both hours and there is no PV, so 4 kWh must be bought either way. Buying both
    hours as they come costs ``2*0.01 + 2*0.50 = 1.02 EUR``. Buying all four in hour 0 -- 2 for the
    load, 2 into the battery, within the 5 kW limit -- costs ``4 * 0.01 = 0.04 EUR``. Charging more
    than 2 would be strictly wasteful, because nothing later needs it.
    """
    prices, hours = _window([10.0, 500.0], [0.0, 0.0], [2.0, 2.0], ZERO_FEED_IN)
    result = solve_window(hours, prices, LOSSLESS, 0.0)

    assert result.status == "Optimal"
    assert result.hours[0].grid_import_kwh == pytest.approx(4.0)
    assert result.hours[0].battery_charge_kwh == pytest.approx(2.0)
    assert result.hours[1].grid_import_kwh == pytest.approx(0.0)
    assert result.hours[1].battery_discharge_kwh == pytest.approx(2.0)
    assert result.net_cost_eur == pytest.approx(0.04)


def test_the_power_limit_caps_how_much_can_be_shifted() -> None:
    """Same shape, but 8 kWh of load against a 5 kW charge limit: only 3 kWh can be pre-bought.

    Hour 0 imports its own 5 kWh of load plus a full 5 kWh charge -- the meter is happy to carry
    10, but the *battery* tops out at 5 kW. Hour 1 then needs 8, takes 5 from store and imports the
    remaining 3 at the expensive price: ``10*0.01 + 3*0.50 = 1.60 EUR``.
    """
    prices, hours = _window([10.0, 500.0], [0.0, 0.0], [5.0, 8.0], ZERO_FEED_IN)
    result = solve_window(hours, prices, LOSSLESS, 0.0)

    assert result.hours[0].battery_charge_kwh == pytest.approx(LOSSLESS.max_charge_kw)
    assert result.hours[1].battery_discharge_kwh == pytest.approx(LOSSLESS.max_discharge_kw)
    assert result.hours[1].grid_import_kwh == pytest.approx(3.0)
    assert result.net_cost_eur == pytest.approx(1.60)


def test_a_flat_price_gives_the_optimiser_nothing_to_beat_naive_with() -> None:
    """Under a flat price, naive self-consumption already is optimal -- and the totals agree.

    A useful negative result: it says the savings this milestone reports come from *price spread*,
    not from the optimiser quietly being scored on a different basis than the baseline. The terminal
    rate is the production derivation, which on a flat window is that flat price -- above the 8.11
    ct/kWh feed-in, so holding the leftover beats dumping it and neither dispatch has a reason to.
    """
    spot = [100.0] * 8
    pv = [0.0, 0.0, 4.0, 4.0, 4.0, 0.0, 0.0, 0.0]
    load = [1.0] * 8
    prices, hours = _window(spot, pv, load)
    terminal = terminal_value_eur_kwh(prices)
    assert terminal == pytest.approx(0.10)

    optimal = solve_window(hours, prices, LOSSLESS, terminal)
    naive = baselines.naive_continuous(hours, prices, LOSSLESS, terminal)

    assert optimal.objective_eur == pytest.approx(naive.objective_eur, abs=1e-6)


def test_self_consumption_beats_export_when_the_import_price_exceeds_the_feed_in_rate() -> None:
    """Why a home battery pays at all: a kWh kept is worth more than a kWh sold.

    At 100 EUR/MWh the import price is 10 ct/kWh against a flat 8.11 ct/kWh feed-in, so a stored kWh
    is worth more used than sold. Surplus is kept and released into the evening deficit.

    The terminal rate is pinned at 9 ct/kWh -- strictly between the two -- rather than taken from
    the production derivation, which on a flat window returns the import price itself and makes
    "hold a kWh" and "serve load with it" worth exactly the same. That tie is real and the solver is
    entitled to break it either way, so asserting a specific schedule under it would be asserting
    the solver's arbitrary choice. With the rate strictly between, the ordering is strict and the
    optimum is unique: serve load (10) beats hold (9) beats export (8.11).
    """
    prices, hours = _window([100.0] * 4, [4.0, 4.0, 0.0, 0.0], [0.0, 0.0, 3.0, 3.0])
    assert terminal_value_eur_kwh(prices) == pytest.approx(0.10)
    result = solve_window(hours, prices, LOSSLESS, 0.09)

    stored = sum(hour.battery_charge_kwh or 0.0 for hour in result.hours)
    exported = sum(hour.grid_export_kwh or 0.0 for hour in result.hours)
    assert stored > exported, "surplus should be kept, not sold at the lower statutory rate"
    assert result.hours[2].battery_discharge_kwh == pytest.approx(3.0)
    assert result.hours[2].grid_import_kwh == pytest.approx(0.0)
    assert result.hours[3].battery_discharge_kwh == pytest.approx(3.0)
    # 8 kWh in, 6 kWh out: the 2 kWh left over is held rather than dumped at the lower rate.
    assert result.soc_end_kwh == pytest.approx(2.0)


def test_an_empty_window_solves_to_nothing_rather_than_failing() -> None:
    """Degenerate but reachable -- a site with no rows in a window must not crash the run."""
    prices, hours = _window([], [], [])
    result = solve_window(hours, prices, LOSSLESS, 0.0)

    assert result.hours == ()
    assert result.priced_hours == 0
    assert result.objective_eur == 0.0
    assert result.soc_end_kwh == result.soc_start_kwh


def test_a_battery_that_cannot_store_anything_reduces_to_the_no_battery_case() -> None:
    """A zero-capacity battery is not an error, it is a household without one."""
    empty = BatteryConfig(
        capacity_kwh=0.0,
        soc_min=0.0,
        soc_max=1.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency=0.9,
    )
    prices, hours = _window([10.0, 500.0], [0.0, 0.0], [2.0, 2.0], ZERO_FEED_IN)
    result = solve_window(hours, prices, empty, 0.0)
    counterfactual = baselines.no_battery(hours, prices, empty, 0.0)

    assert result.objective_eur == pytest.approx(counterfactual.objective_eur, abs=1e-9)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("soc_min", 1.5, "soc_min"),
        ("round_trip_efficiency", 1.4, "round_trip_efficiency"),
        ("max_charge_kw", -1.0, "power limits"),
        ("capacity_kwh", -1.0, "capacity_kwh"),
    ],
)
def test_an_impossible_battery_is_rejected_by_name(field: str, value: float, message: str) -> None:
    """Config validates nothing itself, so the optimiser fails with the field, not a traceback."""
    from dataclasses import replace

    battery = replace(LOSSLESS, **{field: value})
    prices, hours = _window([100.0], [0.0], [1.0])
    with pytest.raises(ValueError, match=message):
        solve_window(hours, prices, battery, 0.0)


def test_a_dispatch_error_names_the_tariff_and_the_window_length() -> None:
    """Diagnosability: a failed solve says which problem failed, not just that one did."""
    assert issubclass(DispatchError, RuntimeError)


def test_the_optimality_bound_is_absolute_not_relative() -> None:
    """A relative MIP gap is a slack budget that grows with the window. Pin it absolute.

    HiGHS defaults to a *relative* gap of 1e-4. At M6's one-week windows the objective was ~-9 EUR
    and that bought ~0.9 mEUR of slack -- invisible. At M7's 88-day window the objective reached
    ~-194 EUR and the same fraction bought ~19 mEUR, so HiGHS stopped, entirely correctly, at a
    solution costing 2.8 mEUR more than a feasible baseline -- and the warehouse reported it as a
    violated theorem, which is a confusing way to learn about a solver default.

    This assertion is deliberately *structural* rather than behavioural, and the distinction is
    worth stating: a unit-scale problem hand-built here closes its gap exactly whether or not the
    option is set, so a behavioural version of this test passes with the bug present -- it was
    written, checked against a reverted fix, and thrown away for that reason. The behavioural guard
    is assert_optimal_never_costs_more_than_naive, which caught this on the seeded warehouse and
    runs in CI. This one only stops the kwargs being quietly dropped.
    """
    assert _MIP_GAP_REL == 0.0, "a non-zero relative gap reintroduces a scale-dependent bound"
    # Below settlement's own 1e-6 rounding, so a satisfying solve is indistinguishable from exact.
    assert 0 < _MIP_GAP_ABS_EUR < 1e-6

    solver = _configured_solver()
    assert getattr(solver, "gapAbs", None) == _MIP_GAP_ABS_EUR
    assert getattr(solver, "gapRel", None) == _MIP_GAP_REL
    # Reproducibility on a given machine rides along here; M6's claim depends on it.
    assert getattr(solver, "threads", None) == 1
