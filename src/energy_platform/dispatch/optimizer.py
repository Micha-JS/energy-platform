"""Hindsight-optimal battery dispatch as a MILP, solved with HiGHS.

One battery, one meter, hourly steps over a coverage window. The decision variables are the AC-side
charge and discharge energies; grid import and export follow from the node identity; state of
charge threads through the window with the round-trip legs applied. The objective is the money the
M5 tariff engine says those flows cost, minus what the energy still in the battery at the end is
worth.

**Why this is a MILP and not an LP.** Negative day-ahead prices let a linear program buy things the
hardware cannot sell it. Two binaries per hour close that off; they are justified differently, and
the difference is worth being straight about.

*Meter exclusivity is load-bearing, and it is the sharper of the two.* Feed-in is a **flat**
statutory rate that does not track the market. So the moment the retail import price drops below
that flat rate, importing and exporting the same kWh is paid at both ends and nothing bounds the
loop -- the program is genuinely **unbounded**. No battery is involved, so no amount of battery-side
post-checking would ever have caught it, and there is no optimum to repair towards. With this
catalogue the crossover is ``spot = -123.75 EUR/MWh``: not the deep tail, just an ordinary bad
afternoon. ``tests/dispatch/test_negative_prices.py`` derives that number from the seed and shows
the naive LP bounded five euros above it and unbounded five below.

*Battery exclusivity is a guarantee, not a fix for a wrong answer.* Once the meter is exclusive,
charging and discharging in the same hour is exactly **objective-neutral**: in import mode the
metered flow is ``load + c - pv - d`` and in export mode ``pv + d - load - c``, so raising both legs
together moves the meter not at all while strictly costing state of charge through the round-trip
legs. It is weakly dominated at every price, and the test asserts that dropping this binary alone
leaves the optimal *value* unchanged. What is left is degeneracy -- it remains reachable as a tie,
and which vertex a simplex returns among equally-optimal ones is not something a model gets to
promise. The binary converts "the solver happens not to" into "the formulation does not permit it".

Neither fires on the shipped demo data, whose seeded windows bottom out at -59.92 EUR/MWh. Both sit
inside the -500..4000 range the warehouse declares acceptable and the M5 property tests generate
over. A guard that only holds for the data we happen to ship is not a guard.

At a week of hours this is ~336 binaries, which HiGHS closes in milliseconds, so correctness by
construction costs nothing here. The big-M coefficients come from the hour's own PV and load rather
than an arbitrary large constant, so the relaxation stays tight -- and they are attached to the
exclusivity constraints only, never duplicated onto the variable bounds, because a meter that may
run both ways at once has no such bound and duplicating it would quietly hide the pathology.

**Determinism.** The optimal *value* is unique; the optimal *schedule* generally is not -- two
hours at the same price are interchangeable. ``threads=1`` keeps a given machine reproducible, but
no claim is made across platforms or solver versions. See
:mod:`energy_platform.dispatch.store` for what that means for the persisted table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Final

import pulp

from energy_platform.config import BatteryConfig
from energy_platform.dispatch.baselines import discharge_efficiency, initial_soc_kwh
from energy_platform.dispatch.model import DispatchResult, HourFlows, HourInputs, Scenario
from energy_platform.dispatch.pricing import WindowPrices, settle_window

SOLVER: Final = "highs"

# How far a solver number may sit from the physics before it stops being float noise and starts
# being a wrong answer. Two facts set it:
#
#   * HiGHS works to a primal feasibility tolerance, so a constraint the model states exactly comes
#     back satisfied to roughly 1e-5 kWh on a degenerate problem -- and dispatch problems are richly
#     degenerate, because equally-priced hours are interchangeable.
#   * The schedule is persisted at 1 Wh (1e-3 kWh), M3's meter resolution.
#
# 0.1 Wh sits an order of magnitude below what is ever stored and comfortably above what the solver
# guarantees. Below it, a value IS the zero the binaries already forced; above it, the solution does
# not close and is raised rather than repaired.
_SOLVER_TOLERANCE_KWH: Final = 1e-4

# How close to proven-optimal a solve must get before HiGHS is allowed to stop.
#
# HiGHS defaults to a *relative* MIP gap of 1e-4, which is a fixed fraction of the objective and
# therefore a budget that grows with the window. At M6's one-week windows the objective was around
# -9 EUR and the gap allowed ~0.9 mEUR of slack -- invisible. M7's 88-day window pushed the
# objective to -194 EUR, and the same relative gap allowed ~19 mEUR: enough for HiGHS to stop, quite
# correctly, at a solution costing 2.8 mEUR more than naive_continuous, which
# assert_optimal_never_costs_more_than_naive then reported as the theorem violation it was.
#
# So the gap is stated in *money* instead. 1e-7 EUR is below the 1e-6 rounding of settlement, so a
# solve that satisfies it cannot be distinguished from an exact optimum by anything downstream, and
# the bound no longer moves when the window does. The relative gap is pinned to zero so it cannot
# reintroduce a scale-dependent budget behind this one.
#
# This was a latent M6 defect, not an M7 one: it was always wrong to let the optimality bound scale
# with the window, and a longer window is simply what made it visible.
_MIP_GAP_ABS_EUR: Final = 1e-7
_MIP_GAP_REL: Final = 0.0


def _configured_solver() -> pulp.LpSolver:
    """The HiGHS instance every solve uses. One place, so a test can assert on it.

    ``threads=1`` keeps a given machine reproducible; the two gap options keep the optimality bound
    stated in money rather than as a fraction of the objective -- see the constants above.
    """
    return pulp.HiGHS(
        msg=False,
        threads=1,
        gapAbs=_MIP_GAP_ABS_EUR,
        gapRel=_MIP_GAP_REL,
    )


class DispatchError(RuntimeError):
    """Raised when the solver does not return a proven optimum, naming the status it did return."""


@dataclass(frozen=True, slots=True)
class RawSolution:
    """The solver's own numbers, before any snapping -- what the model actually returned.

    Exposed so the negative-price regression test can inspect an LP relaxation's behaviour
    directly. Production code goes through :func:`solve_window`, which validates and normalises.
    """

    status: str
    objective: float | None
    indices: tuple[int, ...]
    charge_kwh: tuple[float, ...]
    discharge_kwh: tuple[float, ...]
    grid_import_kwh: tuple[float, ...]
    grid_export_kwh: tuple[float, ...]


def solver_version() -> str:
    """The HiGHS build in use, recorded with every run so a divergent result can be attributed."""
    try:
        return version("highspy")
    except PackageNotFoundError:  # pragma: no cover - highspy is a hard dependency
        return "unknown"


def solve_raw(
    hours: Sequence[HourInputs],
    prices: WindowPrices,
    battery: BatteryConfig,
    terminal_eur_kwh: float,
    *,
    soc_start_kwh: float | None = None,
    enforce_battery_exclusivity: bool = True,
    enforce_meter_exclusivity: bool = True,
) -> RawSolution:
    """Build and solve the dispatch program, returning the solver's raw values.

    ``soc_start_kwh`` defaults to :func:`initial_soc_kwh`, which is what M6 always solves from and
    what M3's generator starts every day at. M8's rolling simulation passes the previous day's
    *executed* terminal state instead -- see :func:`solve_window` for why the terminal valuation
    survives that and the "vacuous constraint" argument does not.

    The two exclusivity flags can be dropped independently. Neither is a supported production mode:
    they exist so ``tests/dispatch/test_negative_prices.py`` can show what each binary is for, one
    at a time, on prices where the difference is visible. Turning either off admits solutions no
    single battery or single meter could perform.
    """
    _validate(battery)
    start_kwh = _start_soc(battery, soc_start_kwh)
    efficiency = discharge_efficiency(battery)
    soc_min_kwh = battery.soc_min * battery.capacity_kwh
    soc_max_kwh = battery.soc_max * battery.capacity_kwh
    can_store = efficiency > 0.0 and soc_max_kwh > soc_min_kwh

    problem = pulp.LpProblem("battery_dispatch", pulp.LpMinimize)
    soc = [
        problem.add_variable(f"soc_{t}", lowBound=soc_min_kwh, upBound=soc_max_kwh)
        for t in range(len(hours))
    ]

    decisions: list[int] = []
    charge: dict[int, pulp.LpVariable] = {}
    discharge: dict[int, pulp.LpVariable] = {}
    grid_import: dict[int, pulp.LpVariable] = {}
    grid_export: dict[int, pulp.LpVariable] = {}
    objective_terms: list[pulp.LpAffineExpression] = []

    previous: pulp.LpAffineExpression | float = start_kwh

    for index, hour in enumerate(hours):
        import_eur_kwh = prices.import_eur_kwh[index]
        if not hour.has_physics or import_eur_kwh is None:
            # An hour nobody can price is an hour nobody may act in. The battery holds its charge
            # and the grid meets whatever the residual is -- the same rule every baseline follows,
            # which is what keeps them inside this program's feasible set.
            problem += soc[index] == previous, f"soc_idle_{index}"
            previous = soc[index]
            continue

        assert hour.pv_production_kwh is not None  # guaranteed by has_physics
        assert hour.household_load_kwh is not None
        pv, load = hour.pv_production_kwh, hour.household_load_kwh
        max_charge = battery.max_charge_kw if can_store else 0.0
        max_discharge = battery.max_discharge_kw if can_store else 0.0

        c = problem.add_variable(f"charge_{index}", lowBound=0.0, upBound=max_charge)
        d = problem.add_variable(f"discharge_{index}", lowBound=0.0, upBound=max_discharge)
        # Tight, data-derived big-Ms rather than an arbitrary constant: with export shut off, the
        # most that can be imported is the load plus a full charge; with import shut off, the most
        # that can be exported is the PV plus a full discharge.
        #
        # They bound the meter *only* through the exclusivity constraints below, and deliberately
        # not as variable bounds as well. Those two are equivalent while exclusivity is enforced --
        # but a meter that may run both ways at once has no such bound, so duplicating it onto the
        # variables would silently keep the relaxation finite and hide exactly the pathology the
        # binaries exist to prevent. The bound belongs to the constraint that gives it meaning.
        max_import = load + max_charge
        max_export = pv + max_discharge
        i = problem.add_variable(f"import_{index}", lowBound=0.0)
        e = problem.add_variable(f"export_{index}", lowBound=0.0)

        problem += pv + i + d == load + e + c, f"ac_node_{index}"
        # Round-trip losses split symmetrically, exactly as M3's dispatch_hour applies them:
        # charging c at the AC node stores c*sqrt(rte); delivering d removes d/sqrt(rte).
        stored = previous + c * efficiency - d / efficiency if can_store else previous
        problem += soc[index] == stored, f"soc_{index}"

        if enforce_battery_exclusivity:
            battery_mode = problem.add_variable(f"battery_mode_{index}", cat=pulp.LpBinary)
            problem += c <= max_charge * battery_mode, f"charge_xor_{index}"
            problem += d <= max_discharge * (1 - battery_mode), f"discharge_xor_{index}"
        if enforce_meter_exclusivity:
            meter_mode = problem.add_variable(f"meter_mode_{index}", cat=pulp.LpBinary)
            problem += i <= max_import * meter_mode, f"import_xor_{index}"
            problem += e <= max_export * (1 - meter_mode), f"export_xor_{index}"

        objective_terms.append(import_eur_kwh * i - prices.feed_in_eur_kwh * e)
        decisions.append(index)
        charge[index], discharge[index] = c, d
        grid_import[index], grid_export[index] = i, e
        previous = soc[index]

    # Energy still stored when the window closes is credited at what it could actually deliver.
    # Without this the optimiser empties the battery on the last evening and books the proceeds as
    # savings -- an artefact of where the window happens to end, not a decision worth anything.
    terminal = terminal_eur_kwh * efficiency
    problem += pulp.lpSum(objective_terms) - terminal * (previous - start_kwh)

    status = problem.solve(_configured_solver())
    return RawSolution(
        status=pulp.LpStatus[status],
        objective=pulp.value(problem.objective),
        indices=tuple(decisions),
        charge_kwh=tuple(_value(charge[t]) for t in decisions),
        discharge_kwh=tuple(_value(discharge[t]) for t in decisions),
        grid_import_kwh=tuple(_value(grid_import[t]) for t in decisions),
        grid_export_kwh=tuple(_value(grid_export[t]) for t in decisions),
    )


def solve_window(
    hours: Sequence[HourInputs],
    prices: WindowPrices,
    battery: BatteryConfig,
    terminal_eur_kwh: float,
    *,
    soc_start_kwh: float | None = None,
) -> DispatchResult:
    """Solve one window under one consumption tariff and settle the result.

    The solver's charge and discharge are taken as the decision; grid flows and the SoC trajectory
    are then re-derived from them so the persisted schedule satisfies the AC-node identity, the
    import/export exclusivity and SoC continuity *exactly* rather than to solver tolerance. The
    solver's own grid flows are checked against the derived ones first, so this normalises floating
    point rather than papering over a wrong answer -- a real disagreement raises.

    **Starting from a carried state of charge.** ``soc_start_kwh`` defaults to ``soc_min *
    capacity``, which is where M6 and M3 both begin. M8 plans each day from the previous day's
    executed terminal state, so it passes that instead. Only one strand of the M6 reasoning is
    specific to the default and it is the one that was already discarded: ``SoC_end >= SoC_start``
    is vacuous *because* the window starts at the SoC lower bound, and above ``soc_min`` it would
    stop being vacuous and start being a real -- and wrong -- constraint. The terminal *valuation*
    that replaced it does not depend on where the window starts: it credits the delta relative to
    ``soc_start_kwh``, at a rate low enough that acquiring energy purely to be credited for it still
    loses money. So a carried start changes the number this returns, as it must, without changing
    what the number means.

    What the caller keeps responsibility for is comparability. Two scenarios may only be ranked
    against each other if they were solved from the same ``soc_start_kwh`` and settled at the same
    terminal rate, which is why :func:`energy_platform.dispatch.runner.solve` derives both once for
    all its scenarios and why M8's simulation settles its three over one span rather than per day.
    """
    solution = solve_raw(hours, prices, battery, terminal_eur_kwh, soc_start_kwh=soc_start_kwh)
    if solution.status != "Optimal":
        raise DispatchError(
            f"solver returned {solution.status!r} for tariff {prices.tariff_id!r} over "
            f"{len(hours)} hours; expected a proven optimum"
        )

    efficiency = discharge_efficiency(battery)
    soc_min_kwh = battery.soc_min * battery.capacity_kwh
    soc_max_kwh = battery.soc_max * battery.capacity_kwh
    start_kwh = _start_soc(battery, soc_start_kwh)
    soc_kwh = start_kwh

    solved = {
        index: (
            solution.charge_kwh[position],
            solution.discharge_kwh[position],
            solution.grid_import_kwh[position],
            solution.grid_export_kwh[position],
        )
        for position, index in enumerate(solution.indices)
    }

    flows: list[HourFlows | None] = []
    for index, hour in enumerate(hours):
        if index not in solved:
            flows.append(_idle(hour, soc_kwh))
            continue
        raw_charge, raw_discharge, raw_import, raw_export = solved[index]
        charge = _clean(raw_charge, battery.max_charge_kw)
        discharge = _clean(raw_discharge, battery.max_discharge_kw)
        if charge > 0.0 and discharge > 0.0:
            raise DispatchError(
                f"hour {hour.ts_utc.isoformat()} charges {charge:.6f} kWh and discharges "
                f"{discharge:.6f} kWh at once -- the exclusivity binaries did not hold"
            )

        assert hour.pv_production_kwh is not None  # solved hours always have physics
        assert hour.household_load_kwh is not None
        residual = hour.pv_production_kwh + discharge - hour.household_load_kwh - charge
        grid_export = max(residual, 0.0)
        grid_import = max(-residual, 0.0)
        _agree(raw_import, grid_import, hour, "import")
        _agree(raw_export, grid_export, hour, "export")

        stored = (
            soc_kwh + charge * efficiency - discharge / efficiency if efficiency > 0 else soc_kwh
        )
        soc_kwh = _bounded(stored, soc_min_kwh, soc_max_kwh, hour)
        flows.append(
            HourFlows(
                battery_charge_kwh=charge,
                battery_discharge_kwh=discharge,
                grid_import_kwh=grid_import,
                grid_export_kwh=grid_export,
                soc_kwh=soc_kwh,
            )
        )

    return settle_window(
        Scenario.OPTIMAL,
        prices,
        hours,
        flows,
        soc_start_kwh=start_kwh,
        terminal_eur_kwh=terminal_eur_kwh,
        discharge_efficiency=efficiency,
        status=solution.status,
        solver=SOLVER,
    )


def _start_soc(battery: BatteryConfig, soc_start_kwh: float | None) -> float:
    """Resolve the start state, refusing one the battery could not have been in.

    An out-of-band start is not a number to clamp: it means the caller's SoC chain has drifted out
    of the physical band, and silently pulling it back would hide that while producing a schedule
    whose first hour does not continue from where the previous one ended.
    """
    if soc_start_kwh is None:
        return initial_soc_kwh(battery)
    lower = battery.soc_min * battery.capacity_kwh
    upper = battery.soc_max * battery.capacity_kwh
    if not lower - _SOLVER_TOLERANCE_KWH <= soc_start_kwh <= upper + _SOLVER_TOLERANCE_KWH:
        raise DispatchError(
            f"start state of charge {soc_start_kwh:.6f} kWh is outside the battery's band "
            f"[{lower:.6f}, {upper:.6f}]"
        )
    return min(max(soc_start_kwh, lower), upper)


def _idle(hour: HourInputs, soc_kwh: float) -> HourFlows | None:
    """An unpriced hour: the battery holds, the grid takes the whole residual."""
    if not hour.has_physics:
        return None
    assert hour.pv_production_kwh is not None
    assert hour.household_load_kwh is not None
    residual = hour.pv_production_kwh - hour.household_load_kwh
    return HourFlows(
        battery_charge_kwh=0.0,
        battery_discharge_kwh=0.0,
        grid_import_kwh=max(-residual, 0.0),
        grid_export_kwh=max(residual, 0.0),
        soc_kwh=soc_kwh,
    )


def _validate(battery: BatteryConfig) -> None:
    """Reject a battery the program cannot mean anything for, naming the field."""
    if battery.capacity_kwh < 0:
        raise ValueError(f"capacity_kwh must be non-negative, got {battery.capacity_kwh}")
    if battery.soc_min > battery.soc_max:
        raise ValueError(
            f"soc_min ({battery.soc_min}) exceeds soc_max ({battery.soc_max}) -- the usable "
            "band is empty"
        )
    if battery.max_charge_kw < 0 or battery.max_discharge_kw < 0:
        raise ValueError("charge and discharge power limits must be non-negative")
    if not 0.0 <= battery.round_trip_efficiency <= 1.0:
        raise ValueError(
            f"round_trip_efficiency must be a fraction in [0, 1], got "
            f"{battery.round_trip_efficiency}"
        )


def _value(variable: pulp.LpVariable) -> float:
    solved = variable.value()
    return 0.0 if solved is None else float(solved)


def _clean(value: float, limit: float) -> float:
    """Read a solver float as the number the model meant.

    An exclusivity the binaries make exact still arrives as 3e-13, because the solver works to a
    tolerance. Snapping anything under it to a true zero is what lets the persisted schedule satisfy
    "never both legs at once" exactly rather than approximately -- and a tenth of a watt-hour is an
    order of magnitude below the 1 Wh the schedule is stored at anyway. A value *outside* the
    tolerance is a real constraint violation and is raised rather than clamped away.
    """
    if value < -_SOLVER_TOLERANCE_KWH or value > limit + _SOLVER_TOLERANCE_KWH:
        raise DispatchError(f"solver returned {value} outside the bound [0, {limit}]")
    if value < _SOLVER_TOLERANCE_KWH:
        return 0.0
    return min(value, limit)


def _agree(solved: float, derived: float, hour: HourInputs, side: str) -> None:
    if abs(solved - derived) > _SOLVER_TOLERANCE_KWH:
        raise DispatchError(
            f"hour {hour.ts_utc.isoformat()}: solver {side} {solved:.9f} kWh disagrees with the "
            f"AC node's {derived:.9f} kWh -- the solution does not close"
        )


def _bounded(soc_kwh: float, lower: float, upper: float, hour: HourInputs) -> float:
    if soc_kwh < lower - _SOLVER_TOLERANCE_KWH or soc_kwh > upper + _SOLVER_TOLERANCE_KWH:
        raise DispatchError(
            f"hour {hour.ts_utc.isoformat()}: state of charge {soc_kwh:.9f} kWh left "
            f"[{lower:.9f}, {upper:.9f}]"
        )
    return min(max(soc_kwh, lower), upper)
