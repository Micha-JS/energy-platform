"""Objective coefficients, taken from the M5 tariff engine rather than restated.

The optimiser needs a price per kWh per hour, in EUR, on both sides of the meter. Every number
here comes from :mod:`energy_platform.tariffs.engine`; this module only converts ct/kWh to EUR/kWh
and decides what stored energy is worth at the end of the window. **No price arithmetic is written
here** -- if it were, it would be a third implementation of the tariff stack alongside the engine
and the dbt macro, and the reconciliation test would not cover it.

**Negative prices are in contract and are not clamped.** A dynamic import price below zero means
the household is paid to consume, which is arithmetic rather than an error; feed-in is a flat
statutory rate that does not track the market and so stays positive in the same hour. The optimiser
relies on both facts, and on the binaries in :mod:`energy_platform.dispatch.optimizer` to stop it
from exploiting the combination in ways one meter and one battery physically cannot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from energy_platform.dispatch.model import (
    DispatchHour,
    DispatchResult,
    HourFlows,
    HourInputs,
    Scenario,
    round_eur,
)
from energy_platform.tariffs.catalog import TariffKind, TariffSpec
from energy_platform.tariffs.engine import CT_PER_EUR, import_price_ct_kwh

TERMINAL_VALUE_ENV: Final = "ENERGY_DISPATCH_TERMINAL_VALUE_CT_KWH"


@dataclass(frozen=True, slots=True)
class WindowPrices:
    """Per-hour objective coefficients for one window under one consumption tariff.

    ``import_eur_kwh[i]`` is ``None`` exactly where the tariff cannot price hour ``i`` -- a dynamic
    tariff with no day-ahead spot for that hour. Those hours are excluded from the objective and
    from every scenario's battery decisions, so that the baselines stay inside the optimiser's
    feasible set and the "optimal is never worse" comparison keeps its footing.
    """

    tariff_id: str
    import_eur_kwh: tuple[float | None, ...]
    feed_in_eur_kwh: float
    import_price_ct_kwh: tuple[float | None, ...]

    def is_priced(self, index: int) -> bool:
        return self.import_eur_kwh[index] is not None


def window_prices(
    spec: TariffSpec, feed_in: TariffSpec, hours: Sequence[HourInputs]
) -> WindowPrices:
    """Price every hour of a window under one consumption tariff and one feed-in scheme."""
    if not spec.is_consumption:
        raise ValueError(
            f"tariff {spec.tariff_id!r} is a {spec.kind.value} row and cannot price consumption"
        )
    if feed_in.kind is not TariffKind.FEED_IN:
        raise ValueError(
            f"tariff {feed_in.tariff_id!r} is a {feed_in.kind.value} row and cannot compensate "
            "exports"
        )
    assert feed_in.price_ct_kwh is not None  # guaranteed by TariffSpec.__post_init__

    ct_kwh = tuple(import_price_ct_kwh(spec, hour.price_eur_mwh) for hour in hours)
    return WindowPrices(
        tariff_id=spec.tariff_id,
        import_eur_kwh=tuple(None if p is None else p / CT_PER_EUR for p in ct_kwh),
        feed_in_eur_kwh=feed_in.price_ct_kwh / CT_PER_EUR,
        import_price_ct_kwh=ct_kwh,
    )


def terminal_value_eur_kwh(prices: WindowPrices, override_ct_kwh: float | None = None) -> float:
    """What a kWh still *stored* at the end of the window is worth, in EUR per delivered kWh.

    Hindsight optimisation with no terminal term drains the battery to ``soc_min`` on the last
    evening: the stored energy was already paid for, so selling or self-consuming it is free money
    that no future hour ever has to replace. The savings that produces are an artefact of where the
    window happens to end.

    The obvious fix -- constraining ``SoC_end >= SoC_start`` -- is **vacuous** when the window
    starts at ``soc_min``, because the SoC lower bound already forces it. It looks like a safeguard
    and does nothing.

    So terminal energy is priced instead, at **the cheapest non-negative import price the window
    offered** -- the lowest rate at which that energy could have been acquired.

    The *mean* import price is the tempting choice and is wrong, in a way worth recording. Charging
    ``c`` at price ``p`` stores ``c*sqrt(rte)`` and, credited at ``lambda*sqrt(rte)``, earns
    ``lambda*rte*c``. So buying purely to end the window full pays whenever ``p < lambda*rte`` --
    which, for a mean, is most hours. The optimiser then hoards: on a test day with one evening
    price spike it charged 14.5 kWh, discharged 2.0, and finished 88% full. That is not a dispatch
    decision, it is the boundary condition paying for itself.

    Taking the minimum makes hoarding provably unprofitable -- acquiring costs at least ``lambda``
    and the credit returns ``lambda*rte < lambda`` -- while any positive value is enough to stop
    the battery being drained for free. The two artefacts are closed from both sides.

    The residual bias is stated rather than hidden: a dispatch that ends with a full battery is
    credited at a **lower bound** on what that energy is worth, so a baseline which finishes full
    is undervalued and the measured savings lean high. Every scenario carries its terminal SoC and
    its credit into ``mart_dispatch_comparison``, so the adjustment can be recomputed at any other
    valuation without re-solving.

    A window in which some hour's *retail* price is itself negative yields zero: energy that could
    have been acquired for less than nothing has no positive lower bound, and crediting it with
    nothing is the conservative reading. That needs ``spot < -191.9 EUR/MWh`` with this catalogue.

    ``override_ct_kwh`` (from ``ENERGY_DISPATCH_TERMINAL_VALUE_CT_KWH``) replaces the derivation
    entirely, for sensitivity analysis.
    """
    if override_ct_kwh is not None:
        return override_ct_kwh / CT_PER_EUR
    priced = [price for price in prices.import_eur_kwh if price is not None]
    if not priced:
        return 0.0
    return max(min(priced), 0.0)


def settle_window(
    scenario: Scenario,
    prices: WindowPrices,
    hours: Sequence[HourInputs],
    flows: Sequence[HourFlows | None],
    *,
    soc_start_kwh: float,
    terminal_eur_kwh: float,
    discharge_efficiency: float,
    status: str,
    solver: str,
) -> DispatchResult:
    """Attach money to one scenario's flows and total the window. The single settlement point.

    Every scenario goes through here, so the four are priced by identical arithmetic and the
    comparison between them cannot be an artefact of one being costed differently from another.

    An hour the tariff cannot price contributes **nothing** -- not a fabricated zero. Its flows are
    still reported (they physically happened), ``is_priced`` is false, and the marts aggregate on
    that flag, so a gap lowers the coverage ratio instead of quietly reading as a free hour.

    Terminal stored energy is credited at ``terminal_eur_kwh * discharge_efficiency``: a kWh in the
    store can only ever reach the AC node multiplied by the discharge leg, so that is what it is
    worth. Applying the credit here rather than inside the solver keeps the reported
    ``objective_eur`` and the solver's own objective the same quantity by construction.
    """
    if not (len(hours) == len(flows) == len(prices.import_eur_kwh)):
        raise ValueError(
            f"length mismatch: {len(hours)} hours, {len(flows)} flows, "
            f"{len(prices.import_eur_kwh)} prices"
        )

    priced_hours = 0
    energy_cost = 0.0
    feed_in_revenue = 0.0
    rows: list[DispatchHour] = []
    soc_end_kwh = soc_start_kwh

    for index, (inputs, flow) in enumerate(zip(hours, flows, strict=True)):
        import_eur = prices.import_eur_kwh[index]
        is_priced = flow is not None and import_eur is not None
        if flow is not None and flow.soc_kwh is not None:
            soc_end_kwh = flow.soc_kwh

        cost = revenue = None
        if is_priced:
            assert flow is not None and import_eur is not None  # narrowed by is_priced
            cost = flow.grid_import_kwh * import_eur
            revenue = flow.grid_export_kwh * prices.feed_in_eur_kwh
            energy_cost += cost
            feed_in_revenue += revenue
            priced_hours += 1

        rows.append(
            DispatchHour(
                ts_utc=inputs.ts_utc,
                pv_production_kwh=inputs.pv_production_kwh,
                household_load_kwh=inputs.household_load_kwh,
                battery_charge_kwh=None if flow is None else flow.battery_charge_kwh,
                battery_discharge_kwh=None if flow is None else flow.battery_discharge_kwh,
                soc_kwh=None if flow is None else flow.soc_kwh,
                grid_import_kwh=None if flow is None else flow.grid_import_kwh,
                grid_export_kwh=None if flow is None else flow.grid_export_kwh,
                price_eur_mwh=inputs.price_eur_mwh,
                import_price_ct_kwh=prices.import_price_ct_kwh[index],
                energy_cost_eur=round_eur(cost),
                feed_in_revenue_eur=round_eur(revenue),
                is_priced=is_priced,
            )
        )

    # Round the three primitives, then derive the two totals *from the rounded values*. Rounding
    # each column independently would leave net_cost_eur a rounding step away from
    # energy_cost_eur - feed_in_revenue_eur, so a reader adding up the columns of a persisted row
    # would not reproduce its own total. Deriving keeps the identities exact:
    #     net_cost_eur == energy_cost_eur - feed_in_revenue_eur
    #     objective_eur == net_cost_eur - terminal_value_eur
    settled_cost = _settled(energy_cost)
    settled_revenue = _settled(feed_in_revenue)
    settled_terminal = _settled(
        terminal_eur_kwh * discharge_efficiency * (soc_end_kwh - soc_start_kwh)
    )
    net_cost = settled_cost - settled_revenue
    return DispatchResult(
        scenario=scenario,
        tariff_id=prices.tariff_id,
        hours=tuple(rows),
        soc_start_kwh=soc_start_kwh,
        soc_end_kwh=soc_end_kwh,
        terminal_value_eur_kwh=terminal_eur_kwh,
        terminal_value_eur=settled_terminal,
        energy_cost_eur=settled_cost,
        feed_in_revenue_eur=settled_revenue,
        net_cost_eur=net_cost,
        objective_eur=net_cost - settled_terminal,
        priced_hours=priced_hours,
        status=status,
        solver=solver,
    )


def _settled(value: float) -> float:
    """Round a window total at the persistence boundary. Never ``None`` -- a total always exists."""
    rounded = round_eur(value)
    assert rounded is not None  # round_eur only returns None for a None input
    return rounded
