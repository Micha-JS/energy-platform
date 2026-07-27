"""Property tests for the invariants every dispatch must satisfy, whatever the prices.

The example tests next door pin specific schedules; these assert what has to hold for *every*
solution the optimiser could return -- including the price shapes nobody thought to write down.
Hypothesis generates the battery, the prices and the profiles together.

* **Energy conservation:** the AC node closes every hour, exactly.
* **State of charge:** never leaves ``[soc_min, soc_max]``, and moves by exactly
  ``charge*sqrt(rte) - discharge/sqrt(rte)``.
* **Exclusivity:** one battery, so never charging and discharging in the same hour; one meter, so
  never importing and exporting in the same hour.
* **Power limits:** neither leg exceeds its kW rating over an hour.
* **Hindsight optimality:** the optimum never costs more than naive self-consumption, and never
  more than having no battery at all.

That last one is a *theorem*, not a hope, and the formulation is what makes it one: both baselines
satisfy every constraint the optimiser solves under and start from the same state of charge, so
they are feasible points of its own problem. A cyclic terminal constraint would have put them
outside the feasible set and left this test asserting something that could legitimately fail.

Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from energy_platform.config import BatteryConfig
from energy_platform.dispatch import baselines
from energy_platform.dispatch.model import DispatchResult, HourInputs, Scenario
from energy_platform.dispatch.optimizer import solve_window
from energy_platform.dispatch.pricing import terminal_value_eur_kwh, window_prices
from tests.dispatch.conftest import BARE_DYNAMIC, FEED_IN, make_hours

# Solving a MILP per example is not free, so the windows are short and the example count modest --
# enough to explore price shapes, few enough to keep `just test` quick. Correctness does not depend
# on window length: the constraints are per-hour and the SoC chain is the only coupling.
_HOURS = 8

# The declared accepted range of the warehouse, negative tail included. The interesting half.
_spot = st.floats(-500.0, 400.0, allow_nan=False)
_energy = st.floats(0.0, 8.0, allow_nan=False)

_batteries = st.builds(
    BatteryConfig,
    capacity_kwh=st.floats(5.0, 30.0),
    soc_min=st.floats(0.0, 0.2),
    soc_max=st.floats(0.8, 1.0),
    max_charge_kw=st.floats(1.0, 10.0),
    max_discharge_kw=st.floats(1.0, 10.0),
    round_trip_efficiency=st.floats(0.5, 1.0),
)

_windows = st.tuples(
    st.lists(_spot, min_size=_HOURS, max_size=_HOURS),
    st.lists(_energy, min_size=_HOURS, max_size=_HOURS),
    st.lists(_energy, min_size=_HOURS, max_size=_HOURS),
)

_slow = settings(deadline=None, max_examples=25, suppress_health_check=[HealthCheck.too_slow])

# Tolerances. Flows are exact float arithmetic, so kWh comparisons are tight. The money identities
# hold exactly (settlement derives the totals from the rounded components), so they get float dust
# only. Cross-scenario cost comparisons budget the full rounding envelope: settlement rounds the
# energy cost, the feed-in revenue and the terminal credit independently at a millionth of a euro,
# so each scenario carries up to +-1.5e-6 and a comparison of two carries +-3e-6, plus snapped flows
# and the terminal SoC quantum. Same derivation, same number as tolerance_eur in
# assert_optimal_never_costs_more_than_naive -- the warehouse and the property tests assert the same
# theorem and must not disagree about what a rounding step costs.
_KWH = 1e-9
_EXACT_EUR = 1e-12
_EUR = 1e-5


def _solve_all(
    prices_load_pv: tuple[list[float], list[float], list[float]], battery: BatteryConfig
) -> dict[Scenario, DispatchResult]:
    spot, pv, load = prices_load_pv
    hours = make_hours(spot, pv, load)
    prices = window_prices(BARE_DYNAMIC, FEED_IN, hours)
    terminal = terminal_value_eur_kwh(prices)
    return {
        Scenario.NO_BATTERY: baselines.no_battery(hours, prices, battery, terminal),
        Scenario.NAIVE_CONTINUOUS: baselines.naive_continuous(hours, prices, battery, terminal),
        Scenario.OPTIMAL: solve_window(hours, prices, battery, terminal),
    }


# -- Property: the AC node closes, every hour, in every scenario -----------------------------


@given(window=_windows, battery=_batteries)
@_slow
def test_every_scenario_conserves_energy_at_the_ac_node(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig
) -> None:
    """pv + import + discharge == load + export + charge, exactly.

    Written out longhand rather than by asking the model, so this is an independent statement of
    the identity and not a restatement of how the flows were built.
    """
    for scenario, result in _solve_all(window, battery).items():
        for hour in result.hours:
            if hour.grid_import_kwh is None:
                continue
            assert hour.pv_production_kwh is not None
            assert hour.household_load_kwh is not None
            assert hour.grid_export_kwh is not None
            assert hour.battery_charge_kwh is not None
            assert hour.battery_discharge_kwh is not None
            supplied = hour.pv_production_kwh + hour.grid_import_kwh + hour.battery_discharge_kwh
            consumed = hour.household_load_kwh + hour.grid_export_kwh + hour.battery_charge_kwh
            assert supplied == pytest.approx(consumed, abs=_KWH), (
                f"{scenario} leaks energy at {hour.ts_utc}"
            )


# -- Property: state of charge stays in bounds and moves only by the round-trip legs ---------


@given(window=_windows, battery=_batteries)
@_slow
def test_state_of_charge_never_leaves_its_bounds(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig
) -> None:
    """A hard constraint, not a preference the objective may buy its way out of."""
    lower = battery.soc_min * battery.capacity_kwh
    upper = battery.soc_max * battery.capacity_kwh
    for scenario, result in _solve_all(window, battery).items():
        if scenario is Scenario.NO_BATTERY:
            continue
        for hour in result.hours:
            assert hour.soc_kwh is not None
            assert lower - _KWH <= hour.soc_kwh <= upper + _KWH, (
                f"{scenario} left the SoC band at {hour.ts_utc}: {hour.soc_kwh}"
            )


@given(window=_windows, battery=_batteries)
@_slow
def test_state_of_charge_moves_only_by_the_round_trip_legs(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig
) -> None:
    """Energy cannot appear in or leave the store except through a charge or a discharge.

    Losses are split symmetrically, exactly as M3's generator applies them: storing ``c`` of AC
    energy adds ``c*sqrt(rte)``, delivering ``d`` removes ``d/sqrt(rte)``.
    """
    efficiency = math.sqrt(battery.round_trip_efficiency)
    for scenario, result in _solve_all(window, battery).items():
        if scenario is Scenario.NO_BATTERY:
            continue
        previous = baselines.initial_soc_kwh(battery)
        for hour in result.hours:
            assert hour.soc_kwh is not None
            assert hour.battery_charge_kwh is not None
            assert hour.battery_discharge_kwh is not None
            expected = (
                previous
                + hour.battery_charge_kwh * efficiency
                - hour.battery_discharge_kwh / efficiency
            )
            assert hour.soc_kwh == pytest.approx(expected, abs=1e-6), (
                f"{scenario} broke SoC continuity at {hour.ts_utc}"
            )
            previous = hour.soc_kwh


# -- Property: one battery, one meter --------------------------------------------------------


@given(window=_windows, battery=_batteries)
@_slow
def test_charge_and_discharge_are_mutually_exclusive(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig
) -> None:
    """The trap this milestone exists to avoid: burning energy through the round-trip losses.

    At a negative import price an LP will happily charge and discharge in the same hour, because
    being paid to consume makes the loss a profit. The binaries make it impossible.
    """
    for scenario, result in _solve_all(window, battery).items():
        for hour in result.hours:
            if hour.battery_charge_kwh is None:
                continue
            assert hour.battery_discharge_kwh is not None
            assert min(hour.battery_charge_kwh, hour.battery_discharge_kwh) == pytest.approx(
                0.0, abs=_KWH
            ), f"{scenario} charges and discharges at once at {hour.ts_utc}"


@given(window=_windows, battery=_batteries)
@_slow
def test_import_and_export_are_mutually_exclusive(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig
) -> None:
    """One meter reads one direction -- the same invariant M5's no-battery counterfactual holds.

    Unbounded without it: once the import price drops below the negative of the flat feed-in rate,
    buying and selling the same kWh pays at both ends and nothing stops the loop.
    """
    for scenario, result in _solve_all(window, battery).items():
        for hour in result.hours:
            if hour.grid_import_kwh is None:
                continue
            assert hour.grid_export_kwh is not None
            assert hour.grid_import_kwh >= -_KWH
            assert hour.grid_export_kwh >= -_KWH
            assert min(hour.grid_import_kwh, hour.grid_export_kwh) == pytest.approx(
                0.0, abs=_KWH
            ), f"{scenario} imports and exports at once at {hour.ts_utc}"


@given(window=_windows, battery=_batteries)
@_slow
def test_power_limits_are_respected_on_both_legs(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig
) -> None:
    """Energy over one hour is power in kW, so the rating is the per-hour kWh ceiling."""
    for scenario, result in _solve_all(window, battery).items():
        for hour in result.hours:
            if hour.battery_charge_kwh is None:
                continue
            assert hour.battery_discharge_kwh is not None
            assert hour.battery_charge_kwh <= battery.max_charge_kw + _KWH, (
                f"{scenario} over-charges at {hour.ts_utc}"
            )
            assert hour.battery_discharge_kwh <= battery.max_discharge_kw + _KWH, (
                f"{scenario} over-discharges at {hour.ts_utc}"
            )


# -- Property: hindsight optimality ----------------------------------------------------------


@given(window=_windows, battery=_batteries)
@_slow
def test_the_optimum_never_costs_more_than_a_feasible_baseline(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig
) -> None:
    """The headline claim, and why the terminal condition is a valuation and not a constraint.

    ``naive_continuous`` and ``no_battery`` both satisfy every constraint the optimiser solves
    under and start from the same state of charge, so each is a feasible point of its problem.
    A minimum cannot exceed the value at a feasible point.
    """
    results = _solve_all(window, battery)
    optimal = results[Scenario.OPTIMAL].objective_eur
    for scenario in (Scenario.NAIVE_CONTINUOUS, Scenario.NO_BATTERY):
        baseline = results[scenario].objective_eur
        assert optimal <= baseline + _EUR, (
            f"the optimum ({optimal}) costs more than {scenario} ({baseline}), so either the "
            "baseline is outside the feasible set or the solver did not find the minimum"
        )


@given(window=_windows, battery=_batteries)
@_slow
def test_the_objective_is_exactly_net_cost_less_the_terminal_credit(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig
) -> None:
    """The adjustment is transparent: a reader can undo it from the persisted columns alone.

    Exact, not approximate. A warehouse row whose own columns do not add up to its own total is a
    row nobody can check, so settlement derives the totals from the rounded components rather than
    rounding each independently.
    """
    for result in _solve_all(window, battery).values():
        assert result.objective_eur == pytest.approx(
            result.net_cost_eur - result.terminal_value_eur, abs=_EXACT_EUR
        )
        assert result.net_cost_eur == pytest.approx(
            result.energy_cost_eur - result.feed_in_revenue_eur, abs=_EXACT_EUR
        )


# -- Property: missing inputs stay missing ---------------------------------------------------


@given(
    battery=_batteries,
    gap=st.integers(min_value=0, max_value=_HOURS - 1),
    prices=st.lists(_spot, min_size=_HOURS, max_size=_HOURS),
)
@_slow
def test_an_unpriceable_hour_idles_the_battery_and_earns_no_money(
    battery: BatteryConfig, gap: int, prices: list[float]
) -> None:
    """NaN over fabrication: an hour without a price is unpriced, never free.

    The battery idles there in every scenario, which is what keeps the baselines inside the
    optimiser's feasible set and the optimality comparison meaningful.
    """
    spot: list[float | None] = list(prices)
    spot[gap] = None
    hours: tuple[HourInputs, ...] = make_hours(spot, [0.0] * _HOURS, [1.0] * _HOURS)
    prices_window = window_prices(BARE_DYNAMIC, FEED_IN, hours)
    terminal = terminal_value_eur_kwh(prices_window)

    for result in (
        baselines.naive_continuous(hours, prices_window, battery, terminal),
        solve_window(hours, prices_window, battery, terminal),
    ):
        hour = result.hours[gap]
        assert hour.is_priced is False
        assert hour.energy_cost_eur is None
        assert hour.feed_in_revenue_eur is None
        assert hour.battery_charge_kwh == pytest.approx(0.0, abs=_KWH)
        assert hour.battery_discharge_kwh == pytest.approx(0.0, abs=_KWH)
        assert result.priced_hours == _HOURS - 1
