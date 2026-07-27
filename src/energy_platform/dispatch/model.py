"""The types the dispatch layer speaks in, and the single quantisation boundary.

Energy is accounted at the **AC coupling point** in kWh, exactly as M3's synthetic generator and
M4's ``stg_telemetry`` do, so the same node identity holds every hour::

    pv + grid_import + battery_discharge == household_load + grid_export + battery_charge

State of charge is *stored* energy in kWh (not the emitted fraction), because the round-trip legs
apply between the AC side and the store: charging ``c`` at the AC node adds ``c*sqrt(rte)`` to the
store, and delivering ``d`` removes ``d/sqrt(rte)`` from it. Keeping SoC in stored kWh is what lets
the optimiser's SoC continuity constraint be written once and read like the physics.

**Two ways an hour can be incomplete, and they are different.** An hour has *physics* when PV and
load are both known -- without them nothing can be simulated at all. An hour is *priced* when, in
addition, the tariff resolves a price for it (a dynamic tariff needs the day-ahead spot; a static
one never does). The distinction mirrors ``int_hourly_tariff_cost``'s ``is_priced`` exactly, which
is what lets the marts aggregate the two layers on the same flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

# Money is rounded at one boundary, to a millionth of a euro. Well below any figure a reader will
# ever see and well above float dust, so a persisted total is stable without pretending to a
# precision the solver does not have. Energy uses M3's 1 Wh quantum (see ``connectors.synthetic``).
_EUR_DP: Final = 6


class Scenario(StrEnum):
    """The four dispatches M6 compares, per coverage window and consumption tariff.

    ``NO_BATTERY`` and ``NAIVE_CONTINUOUS`` are both **feasible points of the optimiser's own
    problem** -- they satisfy every constraint it solves under, starting from the same SoC -- which
    is what makes ``OPTIMAL`` provably no more expensive than either. ``NAIVE_TELEMETERED`` is not:
    M3 simulates each Berlin day independently and resets SoC at midnight, so its trajectory
    violates SoC continuity across day boundaries. It is reported because it is what the warehouse
    actually holds, and deliberately excluded from the optimality assertion because it is not a
    dispatch any single battery could have performed.
    """

    NO_BATTERY = "no_battery"
    NAIVE_TELEMETERED = "naive_telemetered"
    NAIVE_CONTINUOUS = "naive_continuous"
    OPTIMAL = "optimal"


@dataclass(frozen=True, slots=True)
class HourInputs:
    """One hour of warehouse input: the physics drivers, the price, and what M3 actually did.

    Read from ``mart_hourly_energy``, so every field is already NULL wherever a source gapped --
    nothing here fabricates. The ``battery_*`` / ``grid_*`` columns are M3's telemetered flows,
    used only to build the ``naive_telemetered`` scenario; the optimiser and the two derived
    baselines ignore them and re-derive flows from ``pv_production_kwh`` and ``household_load_kwh``.
    """

    ts_utc: datetime
    pv_production_kwh: float | None
    household_load_kwh: float | None
    price_eur_mwh: float | None
    battery_charge_kwh: float | None = None
    battery_discharge_kwh: float | None = None
    grid_import_kwh: float | None = None
    grid_export_kwh: float | None = None
    soc_frac: float | None = None

    @property
    def has_physics(self) -> bool:
        """Whether PV and load are both known, so an hour can be simulated at all."""
        return self.pv_production_kwh is not None and self.household_load_kwh is not None


@dataclass(frozen=True, slots=True)
class HourFlows:
    """One hour's AC-side flows in kWh, plus stored energy at the end of the hour.

    The unpriced intermediate every scenario produces before money is attached: the naive policies
    and the no-battery counterfactual build it directly, the optimiser reads it out of the solved
    model, and :func:`energy_platform.dispatch.pricing.settle_window` turns any of them into
    :class:`DispatchHour` rows. ``soc_kwh`` is ``None`` only for a scenario with no battery.
    """

    battery_charge_kwh: float
    battery_discharge_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    soc_kwh: float | None


@dataclass(frozen=True, slots=True)
class DispatchHour:
    """One hour of one scenario's dispatch, priced. Grain: (ts_utc, scenario, tariff).

    ``soc_kwh`` is stored energy at the *end* of the hour, matching M3's emitted ``soc`` series.
    It is ``None`` for :attr:`Scenario.NO_BATTERY`, which has no battery to have a state.
    """

    ts_utc: datetime
    pv_production_kwh: float | None
    household_load_kwh: float | None
    battery_charge_kwh: float | None
    battery_discharge_kwh: float | None
    soc_kwh: float | None
    grid_import_kwh: float | None
    grid_export_kwh: float | None
    price_eur_mwh: float | None
    import_price_ct_kwh: float | None
    energy_cost_eur: float | None
    feed_in_revenue_eur: float | None
    is_priced: bool


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """One solved scenario over one coverage window under one consumption tariff.

    ``objective_eur`` is the quantity the optimiser minimises and the only figure the four
    scenarios may be ranked by::

        objective_eur == net_cost_eur - terminal_value_eur

    where ``terminal_value_eur`` prices the stored energy the window ends with relative to the
    energy it started with. Without that term, hindsight optimisation drains the battery to
    ``soc_min`` on the last evening and books the proceeds as savings; with it, energy left in the
    battery is worth what it can deliver, so ending full and ending empty are compared honestly.
    ``net_cost_eur`` is kept alongside, and ``soc_end_kwh`` and ``terminal_value_eur_kwh`` beside
    it, so the adjustment can be undone or recomputed at a different valuation.
    See :func:`energy_platform.dispatch.pricing.terminal_value_eur_kwh` for how the rate is chosen
    and which way its bias runs.
    """

    scenario: Scenario
    tariff_id: str
    hours: tuple[DispatchHour, ...]
    soc_start_kwh: float
    soc_end_kwh: float
    terminal_value_eur_kwh: float
    terminal_value_eur: float
    energy_cost_eur: float
    feed_in_revenue_eur: float
    net_cost_eur: float
    objective_eur: float
    priced_hours: int
    status: str
    solver: str

    @property
    def battery_charge_kwh(self) -> float:
        return sum(h.battery_charge_kwh or 0.0 for h in self.hours)

    @property
    def battery_discharge_kwh(self) -> float:
        return sum(h.battery_discharge_kwh or 0.0 for h in self.hours)


def round_eur(value: float | None) -> float | None:
    """Quantise money at the single persistence boundary. ``None`` passes through.

    Normalises ``-0.0`` to ``0.0`` for the same reason M3's ``emit`` does: a signed zero in a
    persisted column is a difference that means nothing and shows up in every diff.
    """
    if value is None:
        return None
    rounded = round(value, _EUR_DP)
    return rounded if rounded != 0 else 0.0
