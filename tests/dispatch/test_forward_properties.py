"""Property tests for the forward simulation: what must hold however wrong the forecast was.

The sibling file next door does this for M6's hindsight optimum. Here the trajectory is not a
solution to anything -- it is a plan clipped to feasibility against a day that disagreed with it --
so the invariants matter more, not less: there is no solver to have enforced them.

Hypothesis generates the battery, the prices, the profiles **and the forecast error** together, so
the plans under test are genuinely infeasible against the actuals rather than politely wrong.

* **Energy conservation:** the AC node closes every executed hour, exactly.
* **State of charge:** never leaves ``[soc_min, soc_max]`` under clipping, and moves only by the
  round-trip legs.
* **Exclusivity:** clipping cannot create a simultaneous charge/discharge or import/export.
* **Power limits:** neither leg exceeds its rating, whatever the plan asked for.
* **SoC chaining:** each day starts where the previous day *executed*, never where it planned.
* **Hindsight optimality:** the optimum never costs more than the executed trajectory, and never
  more than naive.

WHAT IS ASSERTED AND WHAT IS ONLY REPORTED -- the point of this file.

``optimal <= forecast_driven`` is a **theorem** and is asserted: the executed trajectory satisfies
every constraint the optimiser solves under, starts from the same state of charge, and idles in the
same hours, so it is a feasible point of the optimiser's own problem. Clipping a plan to feasibility
cannot leave the feasible set.

``forecast_driven <= naive_continuous`` is **not a theorem and is never asserted**. Naive
self-consumption is reactive and needs no forecast; a day-ahead plan commits in advance and is wrong
whenever the forecast is. A bad forecast can and should lose. There is a test below that constructs
exactly that case and asserts the *reporting* of it -- because a suite that asserted the ordering
would fail precisely when the platform produced its most honest output, and would put quiet pressure
on whoever saw it red to improve the seeded data until it passed.

Nothing here touches Postgres or the network, and nothing fits a model.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from energy_platform.config import BatteryConfig
from energy_platform.dispatch import forward
from energy_platform.dispatch.execution import PlannedHour, execute_plan
from energy_platform.dispatch.model import HourInputs, Scenario
from energy_platform.dispatch.pricing import window_prices
from energy_platform.dispatch.windows import CoverageWindow
from tests.dispatch.conftest import (
    BARE_DYNAMIC,
    FEED_IN,
    LOSSLESS,
    MAY_FIRST_BERLIN,
    berlin_days,
    serving_plan,
    simulated_hours,
)

# Two days is enough to exercise the midnight chain and keeps a MILP-per-example affordable: the
# constraints are per-hour and the SoC chain is the only coupling, so correctness does not depend on
# the number of days. Three would triple the solve time and test nothing new.
_DAYS = 2
_HOURS = _DAYS * 24

_spot = st.floats(-500.0, 400.0, allow_nan=False)
_energy = st.floats(0.0, 6.0, allow_nan=False)

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

# The forecast error. Spans both directions and reaches far enough to make plans genuinely
# infeasible -- a bias near 1.0 would produce plans that execute cleanly and test nothing about
# recourse.
_bias = st.floats(0.2, 2.5)

# Each example solves FIVE MILPs -- two daily plans, two for the perfect-foresight reference, and
# one over the whole span -- so the example count is far more expensive here than in
# test_properties.py, where an example is one solve over eight hours. Eight examples of a two-day
# window explores the price shapes that matter without making `just test` a coffee break;
# correctness does not depend on the count, since the constraints are per-hour and the SoC chain is
# the only coupling.
_slow = settings(deadline=None, max_examples=8, suppress_health_check=[HealthCheck.too_slow])

# Same derivation and the same numbers as test_properties.py, which asserts the same theorem: three
# independently rounded components per scenario at 1e-6 each, two scenarios compared.
_KWH = 1e-9
_EUR = 1e-5


def _simulate(
    window: tuple[list[float], list[float], list[float]],
    battery: BatteryConfig,
    bias: float,
) -> forward.ForwardSolution:
    spot, pv, load = window
    days = berlin_days(_DAYS)
    hours = tuple(
        HourInputs(
            ts_utc=MAY_FIRST_BERLIN + timedelta(hours=index),
            pv_production_kwh=pv[index],
            household_load_kwh=load[index],
            price_eur_mwh=spot[index],
        )
        for index in range(_HOURS)
    )
    outcome = forward.simulate(
        CoverageWindow(days[0], days[-1]),
        "home",
        hours,
        BARE_DYNAMIC,
        FEED_IN,
        battery,
        pv_plan=serving_plan("pv_production_kwh", hours, days, bias=bias),
        load_plan=serving_plan("household_load_kwh", hours, days, bias=bias),
    )
    assert isinstance(outcome, forward.ForwardSolution)
    return outcome


# -- Property: everything the executed trajectory must satisfy, on one simulation ------------


@given(window=_windows, battery=_batteries, bias=_bias)
@_slow
def test_the_executed_dispatch_is_physically_possible(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig, bias: float
) -> None:
    """Every structural invariant, checked on one simulation per example.

    Deliberately one test rather than six. Each example costs five MILP solves -- two daily plans,
    two for the perfect-foresight reference, one for the hindsight span -- so re-simulating the same
    inputs once per invariant made this file take seven minutes to assert things about identical
    objects. The invariants are grouped, the failure messages still name which one broke and where,
    and the suite stays runnable.

    * the AC node closes, exactly, against the REALISED physics -- the invariant recourse is most
      likely to break, because the grid is what absorbs the difference between plan and reality;
    * state of charge stays inside its band under clipping, and moves *only* by the round-trip legs;
    * one battery and one meter: clipping cannot create a simultaneous charge/discharge or
      import/export;
    * neither leg exceeds its rating, whatever the plan asked for;
    * each day starts where the previous day **executed**, never where it planned.
    """
    solution = _simulate(window, battery, bias)
    efficiency = math.sqrt(battery.round_trip_efficiency)
    lower = battery.soc_min * battery.capacity_kwh
    upper = battery.soc_max * battery.capacity_kwh
    previous = solution.execution.soc_start_kwh

    for hour in solution.result(Scenario.FORECAST_DRIVEN).hours:
        if hour.grid_import_kwh is None:
            continue
        assert hour.pv_production_kwh is not None
        assert hour.household_load_kwh is not None
        assert hour.grid_export_kwh is not None
        assert hour.battery_charge_kwh is not None
        assert hour.battery_discharge_kwh is not None
        assert hour.soc_kwh is not None
        at = hour.ts_utc

        # Energy conservation, written out longhand rather than by asking the executor, so this is
        # an independent statement of the identity and not a restatement of how the flows were made.
        supplied = hour.pv_production_kwh + hour.grid_import_kwh + hour.battery_discharge_kwh
        consumed = hour.household_load_kwh + hour.grid_export_kwh + hour.battery_charge_kwh
        assert supplied == pytest.approx(consumed, abs=_KWH), f"energy leaks at {at}"

        # The SoC band, on the far side of the clipping that exists to protect it.
        assert lower - _KWH <= hour.soc_kwh <= upper + _KWH, (
            f"the executed dispatch left the SoC band at {at}: {hour.soc_kwh}"
        )
        expected = (
            previous
            + hour.battery_charge_kwh * efficiency
            - hour.battery_discharge_kwh / efficiency
        )
        assert hour.soc_kwh == pytest.approx(expected, abs=1e-6), f"SoC continuity broke at {at}"
        previous = hour.soc_kwh

        # One battery, one meter. The executor derives both grid legs from a single residual, so
        # exclusivity is structural -- this asserts it survives contact with an infeasible plan.
        assert min(hour.battery_charge_kwh, hour.battery_discharge_kwh) == pytest.approx(
            0.0, abs=_KWH
        ), f"charges and discharges at once at {at}"
        assert hour.grid_import_kwh >= -_KWH
        assert hour.grid_export_kwh >= -_KWH
        assert min(hour.grid_import_kwh, hour.grid_export_kwh) == pytest.approx(0.0, abs=_KWH), (
            f"imports and exports at once at {at}"
        )

        # Power ratings, however much the plan asked for.
        assert hour.battery_charge_kwh <= battery.max_charge_kw + _KWH, f"over-charges at {at}"
        assert hour.battery_discharge_kwh <= battery.max_discharge_kw + _KWH, (
            f"over-discharges at {at}"
        )

    # The milestone's central modelling claim. If a day began from its predecessor's *planned*
    # terminal state, forecast error would be quietly forgiven at every midnight: the simulation
    # would drift further from reality each day while reporting a cost that assumed it had not.
    assert solution.days[0].soc_start_kwh == pytest.approx(
        solution.execution.soc_start_kwh, abs=_KWH
    )
    for earlier, later in zip(solution.days, solution.days[1:], strict=False):
        assert later.soc_start_kwh == pytest.approx(earlier.soc_end_kwh, abs=_KWH), (
            f"the SoC chain broke between {earlier.local_date} and {later.local_date}"
        )
    assert solution.days[-1].soc_end_kwh == pytest.approx(solution.execution.soc_end_kwh, abs=_KWH)


# -- Property: hindsight optimality, which IS a theorem --------------------------------------


@given(window=_windows, battery=_batteries, bias=_bias)
@_slow
def test_the_optimum_never_costs_more_than_what_the_plans_achieved(
    window: tuple[list[float], list[float], list[float]], battery: BatteryConfig, bias: float
) -> None:
    """A theorem, and the reason the recourse policy is a projection rather than a re-plan.

    The executed trajectory obeys the SoC band, both ratings, both exclusivities and the node
    identity, idles in exactly the hours the optimiser must idle in, and starts from the same state
    of charge. So it is a feasible point of the optimiser's own problem, and a minimum cannot exceed
    the value at a feasible point -- for *any* forecast error, which is what the bias strategy is
    exploring. A failure here means recourse left the feasible set, not that the forecast was bad.
    """
    solution = _simulate(window, battery, bias)
    optimal = solution.result(Scenario.OPTIMAL).objective_eur
    for scenario in (
        Scenario.FORECAST_DRIVEN,
        Scenario.PERFECT_FORESIGHT_PLAN,
        Scenario.NAIVE_CONTINUOUS,
    ):
        achieved = solution.result(scenario).objective_eur
        assert optimal <= achieved + _EUR, (
            f"the optimum ({optimal}) costs more than {scenario} ({achieved}), so either that "
            "trajectory left the feasible set or the solver did not find the minimum"
        )


# -- NOT a property: forecast-driven versus naive. Reported, and the report is asserted -------


def test_a_bad_forecast_may_underperform_naive_and_is_reported_not_asserted() -> None:
    """The comparison this suite deliberately refuses to constrain, pinned as *reporting*.

    The construction matters, and getting it wrong first was instructive. On a window with a big
    evening price spike, even a forecast wrong by 3x still *beats* naive dispatch -- because the
    arbitrage is worth so much more than the error costs that "charge at night, discharge at the
    peak" is the right call whatever the sun does. Naive never arbitrages at all, so it loses.

    So the case that makes forecasting lose is a **flat price**. With nothing to arbitrage, naive
    self-consumption is already optimal (M6's finding: under the static tariff the optimum saves
    EUR 0.00), so every deviation a wrong forecast causes is pure loss -- storing energy that should
    have been exported, or exporting energy that should have been stored, at the spread between the
    retail price and the feed-in rate. That is exactly the seeded static-tariff result, where
    forecast-driven dispatch comes out EUR 2.86 behind naive over sixty days.

    This test exists so the absence of an ordering assertion is a decision on the record rather than
    an oversight. If someone later adds ``assert forecast_driven <= naive`` to this file, this test
    is the thing that explains why they should not have.
    """
    days = berlin_days(3)
    # Flat spot: no arbitrage, so naive self-consumption is already the optimum.
    hours = tuple(
        HourInputs(
            ts_utc=hour.ts_utc,
            pv_production_kwh=hour.pv_production_kwh,
            household_load_kwh=hour.household_load_kwh,
            price_eur_mwh=80.0,
        )
        for hour in simulated_hours(days)
    )
    battery = BatteryConfig(
        capacity_kwh=14.0,
        soc_min=0.05,
        soc_max=0.95,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency=0.9,
    )
    solution = forward.simulate(
        CoverageWindow(days[0], days[-1]),
        "home",
        hours,
        BARE_DYNAMIC,
        FEED_IN,
        battery,
        # Believing a third of the real sunshine: the planner leaves the battery empty for a surplus
        # it does not expect, so the surplus is exported at 8.11 ct instead of displacing an import.
        pv_plan=serving_plan("pv_production_kwh", hours, days, bias=0.3),
        load_plan=serving_plan("household_load_kwh", hours, days, bias=0.3),
    )
    assert isinstance(solution, forward.ForwardSolution)

    naive = solution.result(Scenario.NAIVE_CONTINUOUS).objective_eur
    forecast_driven = solution.result(Scenario.FORECAST_DRIVEN).objective_eur
    optimal = solution.result(Scenario.OPTIMAL).objective_eur

    # The theorem still holds -- being wrong does not make the trajectory infeasible.
    assert optimal <= forecast_driven + _EUR
    assert optimal <= naive + _EUR
    # And the empirical result goes the "wrong" way, which is exactly what must not be asserted.
    assert forecast_driven > naive, (
        "this test needs a forecast bad enough to lose to naive dispatch; if the simulation has "
        "become robust enough that a 3x PV error still wins, pick a worse one rather than deleting "
        "the case -- the point is that the suite tolerates losing"
    )
    # The regret is positive and finite, and the share the mart would compute is negative.
    available = naive - optimal
    if available > 1e-6:
        assert (naive - forecast_driven) / available < 0.0


# -- The recourse policy itself, on hand-workable numbers ------------------------------------


def test_a_planned_discharge_larger_than_the_store_is_clipped_to_what_is_there() -> None:
    """The core of the recourse policy, on numbers that can be checked by hand.

    A lossless 10 kWh battery holding 2 kWh, asked to deliver 5. It delivers 2, the grid covers the
    remaining deficit, and the store ends empty rather than negative.
    """
    battery = BatteryConfig(
        capacity_kwh=10.0,
        soc_min=0.0,
        soc_max=1.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency=1.0,
    )
    hours = (
        HourInputs(
            ts_utc=MAY_FIRST_BERLIN,
            pv_production_kwh=0.0,
            household_load_kwh=5.0,
            price_eur_mwh=100.0,
        ),
    )
    prices = window_prices(BARE_DYNAMIC, FEED_IN, hours)
    execution = execute_plan((PlannedHour(0.0, 5.0),), hours, prices, battery, soc_start_kwh=2.0)
    flows = execution.hours[0].flows
    assert flows is not None
    assert flows.battery_discharge_kwh == pytest.approx(2.0, abs=_KWH)
    assert flows.grid_import_kwh == pytest.approx(3.0, abs=_KWH)
    assert flows.grid_export_kwh == pytest.approx(0.0, abs=_KWH)
    assert flows.soc_kwh == pytest.approx(0.0, abs=_KWH)
    assert execution.hours[0].was_clipped is True
    assert execution.hours[0].deviation_kwh == pytest.approx(-3.0, abs=_KWH)


def test_a_planned_charge_with_no_surplus_is_honoured_by_importing() -> None:
    """The case that looks like recourse and is not: the plan is feasible, just expensive.

    The M6 program permits grid charging, so "planned to charge, but the sun did not come out" is
    something the battery can absolutely do -- it imports. Clipping it would be the executor
    substituting its own judgement for the plan's, and would hide the forecast error rather than
    charging for it.
    """
    battery = BatteryConfig(
        capacity_kwh=10.0,
        soc_min=0.0,
        soc_max=1.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency=1.0,
    )
    hours = (
        HourInputs(
            ts_utc=MAY_FIRST_BERLIN,
            pv_production_kwh=0.0,
            household_load_kwh=1.0,
            price_eur_mwh=100.0,
        ),
    )
    prices = window_prices(BARE_DYNAMIC, FEED_IN, hours)
    execution = execute_plan((PlannedHour(3.0, 0.0),), hours, prices, battery, soc_start_kwh=0.0)
    flows = execution.hours[0].flows
    assert flows is not None
    assert flows.battery_charge_kwh == pytest.approx(3.0, abs=_KWH)
    assert flows.grid_import_kwh == pytest.approx(4.0, abs=_KWH)  # 1 kWh load + 3 kWh charge
    assert execution.hours[0].was_clipped is False


def test_an_unpriced_hour_idles_the_battery_whatever_the_plan_said() -> None:
    """The rule that keeps the executed trajectory inside the optimiser's feasible set.

    The optimiser is *constrained* to hold in an hour it cannot price. If the executor acted there,
    the trajectory would be infeasible for the hindsight problem and ``optimal <= forecast_driven``
    would stop being a theorem -- so this is the one place recourse overrides a plan for a reason
    that has nothing to do with the battery.
    """
    battery = BatteryConfig(
        capacity_kwh=10.0,
        soc_min=0.0,
        soc_max=1.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency=1.0,
    )
    hours = (
        HourInputs(
            ts_utc=MAY_FIRST_BERLIN,
            pv_production_kwh=4.0,
            household_load_kwh=1.0,
            price_eur_mwh=None,  # a dynamic tariff with no spot for this hour
        ),
    )
    prices = window_prices(BARE_DYNAMIC, FEED_IN, hours)
    assert not prices.is_priced(0)
    execution = execute_plan((PlannedHour(3.0, 0.0),), hours, prices, battery, soc_start_kwh=5.0)
    flows = execution.hours[0].flows
    assert flows is not None
    assert flows.battery_charge_kwh == pytest.approx(0.0, abs=_KWH)
    assert flows.soc_kwh == pytest.approx(5.0, abs=_KWH)
    assert flows.grid_export_kwh == pytest.approx(3.0, abs=_KWH)


def test_a_day_without_a_forecast_falls_back_to_naive_and_says_so() -> None:
    """A hole in the forecast must not put a hole in the state chain.

    The middle day is unserved. It is dispatched by M3's own self-consumption policy -- which needs
    no forecast, being reactive -- the SoC chain runs through it unbroken, and the day is labelled
    so the mart can report how much of a comparison rested on the fallback rather than on planning.
    """
    days = berlin_days(3)
    hours = simulated_hours(days)
    # LOSSLESS deliberately: this test is about plan status and the continuity of the state chain,
    # neither of which involves round-trip losses, and a lossy battery over a three-day span with a
    # daily price spike turns the hindsight solve into a minute of branch and bound for no extra
    # coverage. The losses are exercised by the hypothesis strategy above.
    battery = LOSSLESS
    missing = days[1]
    solution = forward.simulate(
        CoverageWindow(days[0], days[-1]),
        "home",
        hours,
        BARE_DYNAMIC,
        FEED_IN,
        battery,
        pv_plan=serving_plan("pv_production_kwh", hours, days, skip=[missing]),
        load_plan=serving_plan("household_load_kwh", hours, days, skip=[missing]),
    )
    assert isinstance(solution, forward.ForwardSolution)
    assert solution.simulated_days == 3
    assert solution.fallback_days == 1

    statuses = {day.local_date: day.plan_status for day in solution.days}
    assert statuses[missing] == forward.PLAN_STATUS_FALLBACK
    assert statuses[days[0]] == forward.PLAN_STATUS_PLANNED
    assert statuses[days[2]] == forward.PLAN_STATUS_PLANNED

    for earlier, later in zip(solution.days, solution.days[1:], strict=False):
        assert later.soc_start_kwh == pytest.approx(earlier.soc_end_kwh, abs=_KWH)


def test_a_window_with_no_served_day_is_reported_rather_than_omitted() -> None:
    """M7's precedent: a window that vanishes lets every coverage test pass over nothing."""
    days = berlin_days(2)
    hours = simulated_hours(days)
    outcome = forward.simulate(
        CoverageWindow(days[0], days[-1]),
        "home",
        hours,
        BARE_DYNAMIC,
        FEED_IN,
        BatteryConfig(
            capacity_kwh=10.0,
            soc_min=0.0,
            soc_max=1.0,
            max_charge_kw=5.0,
            max_discharge_kw=5.0,
            round_trip_efficiency=1.0,
        ),
        pv_plan=serving_plan("pv_production_kwh", hours, days, skip=days),
        load_plan=serving_plan("household_load_kwh", hours, days, skip=days),
    )
    assert isinstance(outcome, forward.NotSimulated)
    assert outcome.reason == forward.REASON_NO_FITTED_MODEL


def test_the_simulated_span_starts_at_the_first_served_day() -> None:
    """Warm-up is excluded from the span, so all four scenarios cover identical hours.

    Comparing a forecast-driven result over sixty days against a naive baseline over eighty-eight
    would be the most flattering possible arrangement and the least meaningful. The span is what the
    simulation covered, and every cost the mart reports is over it.
    """
    days = berlin_days(5)
    hours = simulated_hours(days)
    warmed_up = days[2:]
    solution = forward.simulate(
        CoverageWindow(days[0], days[-1]),
        "home",
        hours,
        BARE_DYNAMIC,
        FEED_IN,
        BatteryConfig(
            capacity_kwh=10.0,
            soc_min=0.0,
            soc_max=1.0,
            max_charge_kw=5.0,
            max_discharge_kw=5.0,
            round_trip_efficiency=1.0,
        ),
        pv_plan=serving_plan("pv_production_kwh", hours, warmed_up, skip=[]),
        load_plan=serving_plan("household_load_kwh", hours, warmed_up, skip=[]),
    )
    assert isinstance(solution, forward.ForwardSolution)
    assert solution.sim_start == days[2]
    assert solution.sim_end == days[-1]
    assert solution.simulated_days == 3
    assert solution.expected_hours == 3 * 24
    for scenario in Scenario:
        if scenario in {Scenario.NO_BATTERY, Scenario.NAIVE_TELEMETERED}:
            continue
        assert len(solution.result(scenario).hours) == 3 * 24


def test_the_span_is_measured_by_the_berlin_calendar_across_a_dst_switch() -> None:
    """A 25-hour Sunday must be counted as 25 hours, never as 24.

    The span's expectation comes from ``CoverageWindow.expected_hours`` -- the same helper the dbt
    spine and the coverage macro use -- so this is checking that the simulated span inherits the
    platform's DST handling rather than growing its own.
    """
    october = CoverageWindow(date(2024, 10, 26), date(2024, 10, 28))
    assert october.expected_hours == 25 + 24 + 24
    march = CoverageWindow(date(2024, 3, 30), date(2024, 4, 1))
    assert march.expected_hours == 24 + 23 + 24
