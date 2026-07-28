"""The rolling day-by-day simulation: plan on forecasts, execute against actuals, chain the state.

This is where M6 and M7 meet. Each Berlin day is decided at its own decision time from the
information available then -- day-ahead prices that have already cleared, PV and load that have not
happened yet -- and the resulting schedule is then run against the day that actually occurred. The
battery's state carries from one day to the next along the **executed** trajectory, never the
planned one, so a forecast error on Tuesday is still on the balance sheet on Wednesday.

**Four scenarios, one span, one settlement.** The comparison is only meaningful if all four are
solved over identical hours from an identical starting state and settled at an identical terminal
rate, which is the same discipline ``dispatch.runner.solve`` applies to M6's four:

* ``naive_continuous`` -- M3's self-consumption policy, SoC carried across the span. The baseline.
* ``forecast_driven`` -- what the day-ahead plans actually achieved.
* ``perfect_foresight_plan`` -- the same rolling controller handed the actuals as its forecast.
* ``optimal`` -- the hindsight MILP over the same actuals. The ceiling.

**Why the third one exists.** Without it, everything ``forecast_driven`` falls short of ``optimal``
reads as forecast error, and a good part of it is not. A day-ahead controller decides one day at a
time and cannot move energy across a midnight it has already passed; the hindsight optimum can. On
the seeded spring window that myopia alone costs a real fraction of the total shortfall -- so
quoting one figure without the other would bill a deliberate design choice to the models. The
reference runs through the *identical* loop (see :func:`_roll`), differing only in what it is told
about the weather, so the difference between the two is the forecast and nothing else.

Two orderings hold and only two are asserted anywhere: ``optimal <= naive_continuous`` (M6's
theorem, unchanged) and ``optimal <= forecast_driven`` (because the executed trajectory is a
feasible point of the hindsight problem -- see :mod:`energy_platform.dispatch.execution`; the same
argument covers ``perfect_foresight_plan``). ``forecast_driven`` versus ``naive_continuous`` is
**not** an ordering. A forecast good enough to beat naive self-consumption is an empirical result
about this data, and a bad forecast can legitimately lose; the mart reports the comparison and
nothing asserts it. A test suite that asserted it would fail exactly when the system produced its
most interesting output.

**The terminal condition is split, and the split is the subtle part.** Each daily *plan* is solved
with M6's terminal valuation evaluated over that day's own priced hours -- without it the planner
empties the battery every evening, because within a single day the stored energy is free money.
With it, hoarding stays unprofitable by the same argument M6 makes, now at day grain. The planner is
therefore myopic across midnight, which is exactly what a day-ahead decision is and not a defect to
be engineered away. *Settlement*, by contrast, happens once over the whole span at one rate for all
four scenarios: a per-day or per-scenario terminal credit would let them be paid differently for
the energy they end holding, and the regret figure would be partly an artefact of the adjustment
rather than a measurement of dispatch.

**Three ways a day inside the span can fail to be planned**, and none breaks the chain: no model has
been fitted yet (the simulation simply starts later), the day's forecast is incomplete, or the day's
*actuals* are. The last two fall back to naive self-consumption for that day -- which is what a real
controller without a usable plan does, and which needs no forecast at all -- and record that they
did. Dropping the day would put a hole in the SoC chain; idling the battery would be a worse
controller than the fallback and would misattribute the loss to the forecast.

The third reason is about the *simulation* rather than the controller, and it is why plannability is
decided once in :func:`_plannable_days` and handed to both runs. A day with a missing meter reading
is one the perfect-foresight reference cannot be given a forecast for -- its forecast is the actuals
-- so letting the real run plan it anyway would make the two runs differ in coverage, and the myopia
figure is defined as nothing but the difference between them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from energy_platform.config import BatteryConfig
from energy_platform.connectors.synthetic import dispatch_hour
from energy_platform.dispatch import baselines
from energy_platform.dispatch.decision import (
    assert_prices_published,
    decision_time_ms,
)
from energy_platform.dispatch.execution import ExecutedHour, Execution, PlannedHour, execute_plan
from energy_platform.dispatch.model import DispatchResult, HourInputs, Scenario
from energy_platform.dispatch.optimizer import DispatchError, solve_window
from energy_platform.dispatch.pricing import (
    WindowPrices,
    planning_continuation_eur_kwh,
    settle_window,
    terminal_value_eur_kwh,
    window_prices,
)
from energy_platform.dispatch.windows import CoverageWindow
from energy_platform.forecasting.runner import TARGET_PV
from energy_platform.forecasting.serving import DayForecast, HourForecast, ServingPlan
from energy_platform.orchestration.ingest import BERLIN
from energy_platform.tariffs.catalog import TariffSpec

# What the perfect-foresight reference records as the model that planned it. Not a real model key:
# it names the counterfactual so a reader of derived.forward_dispatch_runs cannot mistake the
# reference for something that was actually fitted and served.
PERFECT_FORESIGHT_MODEL_KEY: Final = "perfect_foresight"

# How a forecast-driven row describes its own production. It is neither an optimisation nor a
# simulation of a policy: it is the recorded outcome of executing plans, so `derived` says so
# rather than borrowing one of M6's words.
EXECUTED: Final = "executed"
_OK: Final = "Executed"

PLAN_STATUS_PLANNED: Final = "planned"
PLAN_STATUS_FALLBACK: Final = "fallback_naive"

# Why a declared window produced no simulation at all. Recorded rather than left as an absence:
# a window that vanishes from the mart lets its coverage test pass over nothing.
REASON_NO_FITTED_MODEL: Final = "no_fitted_model"
REASON_NO_HOURS: Final = "no_hours"


@dataclass(frozen=True, slots=True)
class DayOutcome:
    """One simulated Berlin day: what was decided, what was done, and where the state went."""

    local_date: date
    decision_ms: int
    plan_status: str
    soc_start_kwh: float
    soc_end_kwh: float
    planned_hours: int
    clipped_hours: int
    clamped_forecast_hours: int
    pv_fit_day: date | None
    load_fit_day: date | None

    @property
    def was_planned(self) -> bool:
        return self.plan_status == PLAN_STATUS_PLANNED


@dataclass(frozen=True, slots=True)
class NotSimulated:
    """A declared window the simulation could not cover, and why."""

    window: CoverageWindow
    region: str
    reason: str


@dataclass(frozen=True, slots=True)
class ForwardSolution:
    """One window and tariff: the four scenarios over the simulated span, and the days behind."""

    window: CoverageWindow
    region: str
    tariff_id: str
    sim_start: date
    sim_end: date
    terminal_value_eur_kwh: float
    hours: tuple[HourInputs, ...]
    results: tuple[DispatchResult, ...]
    days: tuple[DayOutcome, ...]
    execution: Execution
    pv_model_key: str
    load_model_key: str

    @property
    def simulated_days(self) -> int:
        return len(self.days)

    @property
    def expected_hours(self) -> int:
        """The Berlin calendar's count for the simulated span, never a count of these rows."""
        return CoverageWindow(self.sim_start, self.sim_end).expected_hours

    @property
    def fallback_days(self) -> int:
        return sum(1 for day in self.days if not day.was_planned)

    def result(self, scenario: Scenario) -> DispatchResult:
        for result in self.results:
            if result.scenario is scenario:
                return result
        raise KeyError(f"{scenario} was not solved for this window")


def local_date_of(ts_utc: datetime) -> date:
    """The Berlin calendar day an hour belongs to -- the grain every window is declared in."""
    return ts_utc.astimezone(BERLIN).date()


def simulate(
    window: CoverageWindow,
    region: str,
    hours: Sequence[HourInputs],
    spec: TariffSpec,
    feed_in: TariffSpec,
    battery: BatteryConfig,
    *,
    pv_plan: ServingPlan,
    load_plan: ServingPlan,
    terminal_override_ct_kwh: float | None = None,
    planning_continuation_ct_kwh: float | None = None,
) -> ForwardSolution | NotSimulated:
    """Roll one window forward day by day under one consumption tariff.

    ``hours`` is the window's actuals, exactly as M6 reads them. The two serving plans supply the
    forecasts; a day is planned only when *both* targets resolved it completely, because a schedule
    is a joint decision over PV and load and half a forecast is not a plan.

    The simulated span starts at the first day both plans can serve and runs to the end of the
    window. It is generally shorter than the declared window -- the models need warm-up -- and every
    number this returns is over the span, not the window, so the four scenarios compare
    like for like.
    """
    by_date = _hours_by_date(hours)
    pv_days, load_days = pv_plan.by_day, load_plan.by_day
    servable = sorted(day for day in by_date if day in pv_days and day in load_days)
    if not by_date:
        return NotSimulated(window, region, REASON_NO_HOURS)
    if not servable:
        return NotSimulated(window, region, REASON_NO_FITTED_MODEL)

    sim_start, sim_end = servable[0], max(by_date)
    sim_days = [day for day in sorted(by_date) if sim_start <= day <= sim_end]
    sim_hours = tuple(hour for day in sim_days for hour in by_date[day])

    # Derived once over the whole span, from the actual prices, and handed to all four scenarios.
    prices = window_prices(spec, feed_in, sim_hours)
    terminal = terminal_value_eur_kwh(prices, terminal_override_ct_kwh)
    # Settlement's rate and the planner's are deliberately different quantities -- see
    # `planning_continuation_eur_kwh`. Using the terminal rate to plan with empties the battery
    # every night, which is measured rather than asserted: it cost EUR 19.5 over the seeded window.
    continuation = planning_continuation_eur_kwh(prices, battery, planning_continuation_ct_kwh)

    # Decided once and handed to both runs, so neither can plan a day the other fell back on.
    plannable = _plannable_days(sim_days, by_date, pv_days, load_days)

    soc_start = baselines.initial_soc_kwh(battery)
    execution, outcomes = _roll(
        sim_days,
        by_date,
        spec,
        feed_in,
        battery,
        soc_start=soc_start,
        pv_days=pv_days,
        load_days=load_days,
        plannable=plannable,
        continuation_eur_kwh=continuation,
    )
    # The same controller, handed the actuals as its forecast. What it still loses against the
    # hindsight optimum is the price of deciding one day at a time; subtracting it from the
    # forecast-driven shortfall is what separates model error from controller design. Without this
    # the headline would blame the forecast for a myopia no forecast could fix.
    oracle_execution, _ = _roll(
        sim_days,
        by_date,
        spec,
        feed_in,
        battery,
        soc_start=soc_start,
        pv_days=_perfect_days(plannable, by_date, pv_plan),
        load_days=_perfect_days(plannable, by_date, load_plan),
        plannable=plannable,
        continuation_eur_kwh=continuation,
    )

    efficiency = baselines.discharge_efficiency(battery)

    def settled(scenario: Scenario, done: Execution) -> DispatchResult:
        return settle_window(
            scenario,
            prices,
            sim_hours,
            done.flows,
            soc_start_kwh=soc_start,
            terminal_eur_kwh=terminal,
            discharge_efficiency=efficiency,
            status=_OK,
            solver=EXECUTED,
        )

    results = (
        baselines.naive_continuous(sim_hours, prices, battery, terminal),
        settled(Scenario.FORECAST_DRIVEN, execution),
        settled(Scenario.PERFECT_FORESIGHT_PLAN, oracle_execution),
        solve_window(sim_hours, prices, battery, terminal),
    )
    return ForwardSolution(
        window=window,
        region=region,
        tariff_id=spec.tariff_id,
        sim_start=sim_start,
        sim_end=sim_end,
        terminal_value_eur_kwh=terminal,
        hours=sim_hours,
        results=results,
        days=tuple(outcomes),
        execution=execution,
        pv_model_key=pv_plan.model_key,
        load_model_key=load_plan.model_key,
    )


def _roll(
    sim_days: Sequence[date],
    by_date: Mapping[date, tuple[HourInputs, ...]],
    spec: TariffSpec,
    feed_in: TariffSpec,
    battery: BatteryConfig,
    *,
    soc_start: float,
    pv_days: Mapping[date, DayForecast],
    load_days: Mapping[date, DayForecast],
    plannable: AbstractSet[date],
    continuation_eur_kwh: float,
) -> tuple[Execution, list[DayOutcome]]:
    """Plan, execute and chain, one Berlin day at a time.

    The whole rolling structure lives here so that the perfect-foresight reference runs through
    *identical* code and differs only in the forecasts it is handed. A second loop written to be
    "the same but with actuals" would eventually stop being the same, and the difference between
    the two runs -- which is exactly what the myopia figure measures -- would silently become a
    difference in controller as well. ``plannable`` is passed in for the same reason: which days
    are plannable is a property of the *simulation*, not of either run's forecasts, so it is
    decided once by :func:`_plannable_days` rather than rediscovered here from whatever this run
    happens to have been handed.

    The full ``pv_days``/``load_days`` are still passed alongside it, and not filtered down to
    ``plannable``, because a day that fell back because its forecast had a hole in it still has a
    fitted model behind it -- ``forward_dispatch_days.pv_fit_day`` should say which one.
    """
    soc_kwh = soc_start
    executed_hours: list[ExecutedHour] = []
    outcomes: list[DayOutcome] = []

    for day in sim_days:
        day_hours = by_date[day]
        decision_ms = decision_time_ms(day)
        # The price-side no-lookahead guard. Planning day D at D 00:00 is fine; the same call on a
        # day whose auction has not cleared raises rather than reading it out of the warehouse.
        assert_prices_published(day, decision_ms)

        day_prices = window_prices(spec, feed_in, day_hours)
        pv_day, load_day = pv_days.get(day), load_days.get(day)
        plan, status, clamped = _plan_for_day(
            day_hours,
            day_prices,
            battery,
            soc_kwh,
            pv_day,
            load_day,
            continuation_eur_kwh,
            plannable=day in plannable,
        )
        execution = execute_plan(plan, day_hours, day_prices, battery, soc_start_kwh=soc_kwh)

        executed_hours.extend(execution.hours)
        outcomes.append(
            DayOutcome(
                local_date=day,
                decision_ms=decision_ms,
                plan_status=status,
                soc_start_kwh=soc_kwh,
                soc_end_kwh=execution.soc_end_kwh,
                planned_hours=len(day_hours),
                clipped_hours=execution.clipped_hours,
                clamped_forecast_hours=clamped,
                pv_fit_day=pv_day.fit_day if pv_day is not None else None,
                load_fit_day=load_day.fit_day if load_day is not None else None,
            )
        )
        soc_kwh = execution.soc_end_kwh

    return (
        Execution(tuple(executed_hours), soc_start_kwh=soc_start, soc_end_kwh=soc_kwh),
        outcomes,
    )


def _plannable_days(
    sim_days: Sequence[date],
    by_date: Mapping[date, tuple[HourInputs, ...]],
    pv_days: Mapping[date, DayForecast],
    load_days: Mapping[date, DayForecast],
) -> frozenset[date]:
    """The days the controller is allowed to plan -- decided once, for **both** runs.

    Three conditions, and the third is the one that is easy to miss. Both forecasts must be present
    and complete, because a schedule is a joint decision over PV and load. *And* the day's actuals
    must be complete, because the perfect-foresight reference's forecast **is** the actuals: a day
    with a hole in them is one the oracle cannot be handed a forecast for at all.

    Deciding this separately per run is the bug this function exists to make impossible. The two
    runs would then differ in *coverage* and not only in accuracy -- one planning a day the other
    fell back on -- and ``mart_dispatch_regret`` defines ``myopia_cost_eur`` as nothing more than
    the difference between them, so a coverage gap would be billed to controller myopia. Both
    directions were reachable: a forecast with one unresolved hour made the real run fall back
    while the oracle planned, and a day with one missing meter reading did the reverse.
    """
    plannable: set[date] = set()
    for day in sim_days:
        pv_day, load_day = pv_days.get(day), load_days.get(day)
        if pv_day is None or load_day is None:
            continue
        if not pv_day.is_complete or not load_day.is_complete:
            continue
        if not all(hour.has_physics for hour in by_date[day]):
            continue
        plannable.add(day)
    return frozenset(plannable)


def _perfect_days(
    plannable: AbstractSet[date],
    by_date: Mapping[date, tuple[HourInputs, ...]],
    plan: ServingPlan,
) -> dict[date, DayForecast]:
    """The actuals dressed as forecasts, for the perfect-foresight reference.

    Exactly the days :func:`_plannable_days` admitted -- no more, so the oracle cannot start
    earlier or skip a fallback the real run took, and no fewer, so it cannot fall back on a day the
    real run planned. Both would confound the myopia figure with a coverage difference.
    """
    served = plan.by_day
    perfect: dict[date, DayForecast] = {}
    for day in sorted(plannable):
        real = served[day]
        hours = by_date[day]
        values = [
            hour.pv_production_kwh if plan.target == TARGET_PV else hour.household_load_kwh
            for hour in hours
        ]
        if any(value is None for value in values):
            # Unreachable: `_plannable_days` admits only days whose actuals are complete. Raised
            # rather than skipped, because skipping is what silently reopens the coverage gap.
            raise DispatchError(f"{plan.target} actuals for {day} are incomplete but plannable")
        perfect[day] = DayForecast(
            target=real.target,
            target_day=day,
            decision_ms=real.decision_ms,
            vintage_issue_ms=real.vintage_issue_ms,
            model_key=PERFECT_FORESIGHT_MODEL_KEY,
            config_hash=real.config_hash,
            fit_day=real.fit_day,
            hours=tuple(
                HourForecast(int(hour.ts_utc.timestamp() * 1000), value, was_clamped=False)
                for hour, value in zip(hours, values, strict=True)
            ),
        )
    return perfect


def _plan_for_day(
    day_hours: Sequence[HourInputs],
    day_prices: WindowPrices,
    battery: BatteryConfig,
    soc_kwh: float,
    pv_day: DayForecast | None,
    load_day: DayForecast | None,
    continuation_eur_kwh: float,
    *,
    plannable: bool,
) -> tuple[tuple[PlannedHour, ...], str, int]:
    """The battery trajectory this day was decided on, and how it was arrived at.

    ``plannable`` is the single source of truth and is not re-derived here from ``pv_day`` and
    ``load_day``: see :func:`_plannable_days` for why a second opinion about it is the confound
    this milestone's headline figure is most vulnerable to.
    """
    if not plannable:
        return _naive_fallback(day_hours, day_prices, battery, soc_kwh), PLAN_STATUS_FALLBACK, 0
    assert pv_day is not None and load_day is not None  # guaranteed by _plannable_days

    forecast_hours = _forecast_inputs(day_hours, pv_day, load_day)
    # `day_prices` is reused rather than re-derived from `forecast_hours`, and the two are the same
    # object for a reason worth stating: prices are actuals at planning time -- this day's auction
    # cleared yesterday lunchtime -- so the planning problem is deterministic in money and uncertain
    # only in physics. Re-pricing the forecast hours would compute the identical numbers while
    # implying the price was somehow forecast too.
    solved = solve_window(
        forecast_hours, day_prices, battery, continuation_eur_kwh, soc_start_kwh=soc_kwh
    )
    plan = tuple(
        PlannedHour(hour.battery_charge_kwh or 0.0, hour.battery_discharge_kwh or 0.0)
        for hour in solved.hours
    )
    clamped = pv_day.clamped_hours + load_day.clamped_hours
    return plan, PLAN_STATUS_PLANNED, clamped


def _forecast_inputs(
    day_hours: Sequence[HourInputs],
    pv_day: DayForecast,
    load_day: DayForecast,
) -> tuple[HourInputs, ...]:
    """The day as the planner sees it: forecast physics, actual prices, no telemetry.

    The telemetered ``battery_*``/``grid_*`` columns are deliberately dropped. They are M3's record
    of what happened, they are only ever read by ``naive_telemetered``, and carrying them into a
    forward-looking object would put the realised day inside the thing that is supposed to be
    predicting it.
    """
    pv_values, load_values = pv_day.values(), load_day.values()
    return tuple(
        HourInputs(
            ts_utc=hour.ts_utc,
            pv_production_kwh=pv_values[index],
            household_load_kwh=load_values[index],
            price_eur_mwh=hour.price_eur_mwh,
        )
        for index, hour in enumerate(day_hours)
    )


def _naive_fallback(
    day_hours: Sequence[HourInputs],
    day_prices: WindowPrices,
    battery: BatteryConfig,
    soc_kwh: float,
) -> tuple[PlannedHour, ...]:
    """What the controller does on a day it has no usable forecast for: self-consume.

    Built from the day's *actuals* rather than from a forecast, and that is correct rather than a
    leak: naive self-consumption is a reactive rule, not a plan. It looks at the meter in the
    current hour and charges the surplus or discharges the deficit, so it needs no knowledge of the
    future at all -- which is exactly why it is the sensible thing to fall back to. M3's own
    ``dispatch_hour`` is called so the fallback is the shipped policy rather than a second opinion
    about it, and the resulting trajectory is feasible by construction, so ``execute_plan`` replays
    it without clipping anything.
    """
    plan: list[PlannedHour] = []
    for index, hour in enumerate(day_hours):
        if not hour.has_physics or not day_prices.is_priced(index):
            plan.append(PlannedHour(0.0, 0.0))
            continue
        assert hour.pv_production_kwh is not None  # guaranteed by has_physics
        assert hour.household_load_kwh is not None
        outcome = dispatch_hour(hour.pv_production_kwh, hour.household_load_kwh, soc_kwh, battery)
        soc_kwh = outcome.soc_kwh
        plan.append(PlannedHour(outcome.charge, outcome.discharge))
    return tuple(plan)


def _hours_by_date(hours: Sequence[HourInputs]) -> dict[date, tuple[HourInputs, ...]]:
    """Group the window's hours into Berlin calendar days, preserving order within each."""
    grouped: dict[date, list[HourInputs]] = {}
    for hour in hours:
        grouped.setdefault(local_date_of(hour.ts_utc), []).append(hour)
    return {day: tuple(day_hours) for day, day_hours in grouped.items()}
