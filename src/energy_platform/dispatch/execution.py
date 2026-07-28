"""What actually happens when a plan made on forecasts meets the day that really occurred.

This is the modelling decision M8 exists to make explicit. A schedule optimised against forecast PV
and forecast load is, in general, not a schedule the real day permits: the sun was dimmer than
predicted and the planned discharge would empty a battery that never got as full as expected. Some
rule has to say what the controller does then, and the rule is not derivable -- it is a choice about
what kind of controller is being simulated.

**The policy: track the planned battery power, clipped hour by hour to what is feasible.**

The battery follows its planned charge and discharge exactly, except where physics forbids it, in
which case it does as much of the plan as it can. The grid then absorbs whatever is left over --
which it always can, being the one flow in this model with no capacity limit. Nothing re-optimises.

**What is actually infeasible is narrower than it first looks.** The instinct is that "planned to
charge, but the sun did not come out" is the failure case. It is not: the M6 program permits grid
charging, so a planned charge with no surplus is perfectly executable -- the household simply
imports to do it, and that is a real decision a real controller would carry out. The plan is
honoured and the money is worse, which is exactly how a forecast error should show up.

Only two things genuinely cannot be done, and both are the store's own limits:

* discharging more than is stored above ``soc_min``;
* charging more than fits below ``soc_max``.

(The power ratings are a third bound, but a plan from the optimiser already respects them; they are
re-applied here so that this function is correct for *any* input trajectory, not only a solved one.)

So execution is a **feasibility projection**, not a re-plan, and the deviation between planned and
executed is a reported quantity rather than something absorbed silently.

**Alternatives, and why not.** Re-optimising each hour on the realised state (model-predictive
control) is the stronger controller and the honest upper bound on one -- but it needs an intraday
forecast update this platform does not ingest, and it would make "forecast-driven" a measurement of
the re-planner rather than of the day-ahead forecast the milestone is about. Tracking the planned
*state of charge* instead of the planned power is the other tempting option, and it is worse: it
makes the battery chase a SoC target through the grid, absorbing every forecast error into cycling,
which is the opposite of what a self-consumption controller does.

**Why the result stays comparable to the optimum.** The executed trajectory obeys the SoC band, the
power ratings, both exclusivities and the AC-node identity, it idles in exactly the hours the
optimiser is constrained to idle in, and it starts from the same state of charge. So it is a
feasible point of the hindsight problem, and ``optimal <= forecast_driven`` is a theorem in the same
way ``optimal <= naive_continuous`` is. That is the one ordering M8 asserts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from energy_platform.config import BatteryConfig
from energy_platform.dispatch.baselines import discharge_efficiency
from energy_platform.dispatch.model import HourFlows, HourInputs
from energy_platform.dispatch.pricing import WindowPrices

# Below this, a departure from the plan is float dust rather than recourse. 0.1 Wh -- the same
# quantum the optimiser calls zero (``optimizer._SOLVER_TOLERANCE_KWH``) and a tenth of the 1 Wh the
# meter reports at.
#
# Not cosmetic. `was_clipped` was an exact float comparison, and the SoC headroom the executor
# recomputes differs from the solver's in the last bits, so a plan that ran *exactly* as written
# still flagged ~25 hours out of 1440 as clipped. That is a metric reporting recourse where none
# happened, on the one column a reader would use to judge whether the plans were feasible at all.
_CLIP_TOLERANCE_KWH: Final = 1e-4


@dataclass(frozen=True, slots=True)
class PlannedHour:
    """One hour of a plan: what the day-ahead solve intended the battery to do.

    AC-side energies in kWh, exactly as :class:`~energy_platform.dispatch.model.HourFlows` reports
    them, so a plan can be read straight off a solved :class:`DispatchResult` without conversion.
    Both zero is a legitimate plan (hold), and is what an unplannable hour contributes.
    """

    battery_charge_kwh: float
    battery_discharge_kwh: float


@dataclass(frozen=True, slots=True)
class ExecutedHour:
    """One hour of realised dispatch, and how far it had to depart from the plan.

    ``flows`` is ``None`` for an hour with no physics -- there is nothing to simulate and nothing is
    fabricated. ``was_clipped`` is false in that case: the plan was not overridden, the hour simply
    did not resolve.
    """

    flows: HourFlows | None
    planned_charge_kwh: float
    planned_discharge_kwh: float
    was_clipped: bool

    @property
    def deviation_kwh(self) -> float:
        """Signed planned-minus-executed battery energy. Zero when the plan ran as written."""
        if self.flows is None:
            return 0.0
        planned = self.planned_charge_kwh - self.planned_discharge_kwh
        executed = self.flows.battery_charge_kwh - self.flows.battery_discharge_kwh
        return planned - executed


@dataclass(frozen=True, slots=True)
class Execution:
    """A plan carried out against one span of actuals."""

    hours: tuple[ExecutedHour, ...]
    soc_start_kwh: float
    soc_end_kwh: float

    @property
    def flows(self) -> tuple[HourFlows | None, ...]:
        """The realised flows, in the shape ``settle_window`` takes."""
        return tuple(hour.flows for hour in self.hours)

    @property
    def clipped_hours(self) -> int:
        return sum(1 for hour in self.hours if hour.was_clipped)


def execute_plan(
    planned: Sequence[PlannedHour],
    actuals: Sequence[HourInputs],
    prices: WindowPrices,
    battery: BatteryConfig,
    *,
    soc_start_kwh: float,
) -> Execution:
    """Run ``planned`` against ``actuals``, clipping to feasibility and closing on the grid.

    ``planned`` and ``actuals`` are aligned index-for-index and must be the same length as
    ``prices`` -- the caller builds all three from one hour spine, and a mismatch is a bug rather
    than something to reconcile here.

    An hour without physics, or one the tariff cannot price, is **idled**: the battery holds and the
    grid meets the whole residual. That is not a convenience, it is the rule the optimiser and both
    derived baselines already follow, and matching it is what keeps this trajectory inside the
    optimiser's feasible set.
    """
    if not (len(planned) == len(actuals) == len(prices.import_eur_kwh)):
        raise ValueError(
            f"length mismatch: {len(planned)} planned hours, {len(actuals)} actual hours, "
            f"{len(prices.import_eur_kwh)} prices"
        )

    efficiency = discharge_efficiency(battery)
    soc_min_kwh = battery.soc_min * battery.capacity_kwh
    soc_max_kwh = battery.soc_max * battery.capacity_kwh
    can_store = efficiency > 0.0 and soc_max_kwh > soc_min_kwh
    soc_kwh = soc_start_kwh

    executed: list[ExecutedHour] = []
    for index, (plan, hour) in enumerate(zip(planned, actuals, strict=True)):
        if not hour.has_physics:
            executed.append(
                ExecutedHour(None, plan.battery_charge_kwh, plan.battery_discharge_kwh, False)
            )
            continue

        assert hour.pv_production_kwh is not None  # guaranteed by has_physics
        assert hour.household_load_kwh is not None

        if not prices.is_priced(index) or not can_store:
            charge = discharge = 0.0
        else:
            charge, discharge = _feasible(
                plan, battery, efficiency, soc_kwh, soc_min_kwh, soc_max_kwh
            )

        was_clipped = (
            abs(charge - plan.battery_charge_kwh) > _CLIP_TOLERANCE_KWH
            or abs(discharge - plan.battery_discharge_kwh) > _CLIP_TOLERANCE_KWH
        )
        residual = hour.pv_production_kwh + discharge - hour.household_load_kwh - charge
        soc_kwh = _step(soc_kwh, charge, discharge, efficiency, soc_min_kwh, soc_max_kwh)
        executed.append(
            ExecutedHour(
                flows=HourFlows(
                    battery_charge_kwh=charge,
                    battery_discharge_kwh=discharge,
                    grid_import_kwh=max(-residual, 0.0),
                    grid_export_kwh=max(residual, 0.0),
                    soc_kwh=soc_kwh,
                ),
                planned_charge_kwh=plan.battery_charge_kwh,
                planned_discharge_kwh=plan.battery_discharge_kwh,
                was_clipped=was_clipped,
            )
        )

    return Execution(tuple(executed), soc_start_kwh=soc_start_kwh, soc_end_kwh=soc_kwh)


def plan_from_flows(flows: Sequence[HourFlows | None]) -> tuple[PlannedHour, ...]:
    """Read a plan off a solved schedule. An hour the solve could not resolve plans to hold."""
    return tuple(
        PlannedHour(0.0, 0.0)
        if flow is None
        else PlannedHour(flow.battery_charge_kwh, flow.battery_discharge_kwh)
        for flow in flows
    )


def _feasible(
    plan: PlannedHour,
    battery: BatteryConfig,
    efficiency: float,
    soc_kwh: float,
    soc_min_kwh: float,
    soc_max_kwh: float,
) -> tuple[float, float]:
    """The most of ``plan`` this hour can actually do, given the store's level and its ratings.

    A plan that asks for both legs at once is refused rather than half-executed: it is not something
    the optimiser can emit (the exclusivity binaries forbid it) and silently picking one leg would
    invent a decision nobody made.
    """
    charge = max(plan.battery_charge_kwh, 0.0)
    discharge = max(plan.battery_discharge_kwh, 0.0)
    if charge > 0.0 and discharge > 0.0:
        raise ValueError(
            f"a plan cannot charge {charge:.6f} kWh and discharge {discharge:.6f} kWh in the same "
            "hour -- one battery, one direction"
        )

    if charge > 0.0:
        headroom_kwh = max(soc_max_kwh - soc_kwh, 0.0)
        return min(charge, battery.max_charge_kw, headroom_kwh / efficiency), 0.0
    if discharge > 0.0:
        available_kwh = max(soc_kwh - soc_min_kwh, 0.0)
        return 0.0, min(discharge, battery.max_discharge_kw, available_kwh * efficiency)
    return 0.0, 0.0


def _step(
    soc_kwh: float,
    charge: float,
    discharge: float,
    efficiency: float,
    soc_min_kwh: float,
    soc_max_kwh: float,
) -> float:
    """Advance stored energy by the round-trip legs, then snap float dust back inside the band.

    The clipping above already keeps the move inside the band in exact arithmetic; this only removes
    the last-bit overshoot that ``x / e * e`` leaves behind, and only ever by that much. It is not a
    safety net -- a real violation cannot reach here, because ``_feasible`` bounded the move.
    """
    stored = soc_kwh + charge * efficiency - discharge / efficiency if efficiency > 0 else soc_kwh
    return min(max(stored, soc_min_kwh), soc_max_kwh)
