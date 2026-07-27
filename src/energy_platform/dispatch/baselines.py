"""The three dispatches the optimum is measured against.

None of the physics or arithmetic here is new. ``no_battery`` wraps M5's
:func:`~energy_platform.tariffs.counterfactual.no_battery_flows`; ``naive_continuous`` replays M3's
:func:`~energy_platform.connectors.synthetic.dispatch_hour` verbatim; ``naive_telemetered`` passes
through what the generator actually emitted. Reusing rather than restating is the point -- a
baseline that reimplemented the naive policy could differ from the shipped one in ways the savings
number would silently absorb.

**Why there are two naive baselines.** M3 simulates each Berlin day independently, from a fixed
initial SoC, because a day's telemetry has to be reproducible from that one partition date alone --
that purity is what makes re-ingestion a verifiable no-op. The cost is that state of charge resets
at every midnight and whatever the battery still held is discarded: over the seeded March window it
charges 54 kWh and discharges 13. Comparing an optimum against *that* measures a modelling
simplification, not optimisation.

So ``naive_continuous`` runs the identical policy with SoC carried across the whole window -- the
same function, the same ``BatteryConfig``, differing only in the boundary condition. It is the
honest baseline and the one the headline savings figure uses. ``naive_telemetered`` is kept beside
it because it is what the warehouse actually holds, and the gap between the two *is*
``battery_unreturned_kwh``.

Both derived baselines idle the battery in any hour the tariff cannot price, exactly as the
optimiser does. That symmetry is not cosmetic: it is what keeps ``naive_continuous`` inside the
optimiser's feasible set, and therefore what makes "the optimum is never more expensive" a
consequence of optimality rather than an empirical hope.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from energy_platform.config import BatteryConfig
from energy_platform.connectors.synthetic import dispatch_hour
from energy_platform.dispatch.model import DispatchResult, HourFlows, HourInputs, Scenario
from energy_platform.dispatch.pricing import WindowPrices, settle_window
from energy_platform.tariffs.counterfactual import no_battery_flows

# What a baseline reports as its "solver": these are simulations, not optimisations. Recorded so a
# row in derived.dispatch_runs says plainly how it was produced.
SIMULATED: str = "simulated"
TELEMETERED: str = "telemetered"
_OK: str = "Simulated"


def initial_soc_kwh(battery: BatteryConfig) -> float:
    """The stored energy every scenario starts a window from.

    ``soc_min * capacity`` -- the same initial state M3's ``simulate_day`` uses, so day one of a
    window begins identically whether the trajectory came from the generator or from here. Every
    scenario shares it, which is what makes the terminal-SoC adjustment comparable across them.
    """
    return battery.soc_min * battery.capacity_kwh


def discharge_efficiency(battery: BatteryConfig) -> float:
    """``sqrt(rte)`` -- the discharge leg, matching M3's symmetric split of round-trip losses."""
    return math.sqrt(battery.round_trip_efficiency)


def no_battery(
    hours: Sequence[HourInputs],
    prices: WindowPrices,
    battery: BatteryConfig,
    terminal_eur_kwh: float,
) -> DispatchResult:
    """The PV-only counterfactual: no storage, so surplus exports and deficit imports.

    Flows come straight from M5's ``no_battery_flows``, which is the same definition
    ``int_hourly_counterfactual`` uses -- deliberately, so the dispatch reconciliation test can
    assert the two layers agree hour by hour.
    """
    flows: list[HourFlows | None] = []
    for hour in hours:
        counterfactual = no_battery_flows(hour.pv_production_kwh, hour.household_load_kwh)
        flows.append(
            None
            if counterfactual is None
            else HourFlows(
                battery_charge_kwh=0.0,
                battery_discharge_kwh=0.0,
                grid_import_kwh=counterfactual.grid_import_kwh,
                grid_export_kwh=counterfactual.grid_export_kwh,
                soc_kwh=None,
            )
        )
    return _settle(Scenario.NO_BATTERY, prices, hours, flows, battery, terminal_eur_kwh, SIMULATED)


def naive_continuous(
    hours: Sequence[HourInputs],
    prices: WindowPrices,
    battery: BatteryConfig,
    terminal_eur_kwh: float,
) -> DispatchResult:
    """Naive self-consumption with state of charge carried across the whole window.

    Calls M3's ``dispatch_hour`` unchanged, so charge and discharge limits, the ``sqrt(rte)`` legs
    and the SoC bounds are the shipped generator's, not a second opinion about them. The only
    difference from the telemetered series is that SoC is threaded through midnight instead of
    being reset.
    """
    soc_kwh = initial_soc_kwh(battery)
    flows: list[HourFlows | None] = []

    for index, hour in enumerate(hours):
        if not hour.has_physics or not prices.is_priced(index):
            # Idles, SoC carried -- the same thing the generator does across a null-irradiance gap,
            # and the same thing the optimiser is constrained to do in an unpriced hour.
            flows.append(_idle(hour, soc_kwh))
            continue
        assert hour.pv_production_kwh is not None  # guaranteed by has_physics
        assert hour.household_load_kwh is not None
        outcome = dispatch_hour(hour.pv_production_kwh, hour.household_load_kwh, soc_kwh, battery)
        soc_kwh = outcome.soc_kwh
        flows.append(
            HourFlows(
                battery_charge_kwh=outcome.charge,
                battery_discharge_kwh=outcome.discharge,
                grid_import_kwh=outcome.grid_import,
                grid_export_kwh=outcome.grid_export,
                soc_kwh=soc_kwh,
            )
        )

    return _settle(
        Scenario.NAIVE_CONTINUOUS, prices, hours, flows, battery, terminal_eur_kwh, SIMULATED
    )


def naive_telemetered(
    hours: Sequence[HourInputs],
    prices: WindowPrices,
    battery: BatteryConfig,
    terminal_eur_kwh: float,
) -> DispatchResult:
    """What M3 actually emitted, passed through and priced by the same engine as everything else.

    Not a feasible trajectory for the optimiser -- SoC jumps back to its initial value at every
    Berlin midnight -- so it is reported but never used as the baseline the optimality property is
    asserted against. Its terminal SoC comes from the telemetered ``soc`` fraction, so the discarded
    energy shows up in the comparison rather than being silently written off.
    """
    flows: list[HourFlows | None] = []
    for hour in hours:
        complete = (
            hour.has_physics
            and hour.grid_import_kwh is not None
            and hour.grid_export_kwh is not None
            and hour.battery_charge_kwh is not None
            and hour.battery_discharge_kwh is not None
        )
        if not complete:
            flows.append(None)
            continue
        assert hour.grid_import_kwh is not None  # narrowed by `complete`
        assert hour.grid_export_kwh is not None
        assert hour.battery_charge_kwh is not None
        assert hour.battery_discharge_kwh is not None
        flows.append(
            HourFlows(
                battery_charge_kwh=hour.battery_charge_kwh,
                battery_discharge_kwh=hour.battery_discharge_kwh,
                grid_import_kwh=hour.grid_import_kwh,
                grid_export_kwh=hour.grid_export_kwh,
                soc_kwh=(None if hour.soc_frac is None else hour.soc_frac * battery.capacity_kwh),
            )
        )
    return _settle(
        Scenario.NAIVE_TELEMETERED, prices, hours, flows, battery, terminal_eur_kwh, TELEMETERED
    )


def _idle(hour: HourInputs, soc_kwh: float) -> HourFlows | None:
    """An hour the battery sits out: grid meets the whole residual, stored energy is untouched."""
    counterfactual = no_battery_flows(hour.pv_production_kwh, hour.household_load_kwh)
    if counterfactual is None:
        return None
    return HourFlows(
        battery_charge_kwh=0.0,
        battery_discharge_kwh=0.0,
        grid_import_kwh=counterfactual.grid_import_kwh,
        grid_export_kwh=counterfactual.grid_export_kwh,
        soc_kwh=soc_kwh,
    )


def _settle(
    scenario: Scenario,
    prices: WindowPrices,
    hours: Sequence[HourInputs],
    flows: Sequence[HourFlows | None],
    battery: BatteryConfig,
    terminal_eur_kwh: float,
    solver: str,
) -> DispatchResult:
    return settle_window(
        scenario,
        prices,
        hours,
        flows,
        soc_start_kwh=initial_soc_kwh(battery),
        terminal_eur_kwh=terminal_eur_kwh,
        discharge_efficiency=discharge_efficiency(battery),
        status=_OK,
        solver=solver,
    )
