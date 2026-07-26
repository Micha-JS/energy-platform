"""The no-battery counterfactual: what the meter would have seen with PV alone.

Strip the battery out of an hour and the grid flows follow from PV and load by themselves --
whatever PV cannot cover is imported, whatever load cannot absorb is exported::

    grid_import = max(load - pv, 0)
    grid_export = max(pv - load, 0)

That is the baseline the tariff counterfactual mart prices against the battery's actual
behaviour. The mirror of this arithmetic lives in ``int_hourly_counterfactual.sql``.

Two invariants hold for every hour and are property-tested:

* **Energy conservation:** ``pv + grid_import == load + grid_export`` exactly. With no storage
  there is nowhere for energy to hide, so this is an identity, not an approximation.
* **Mutual exclusivity:** an hour imports or exports, never both.

Missing inputs propagate as ``None``. The SQL side needs an explicit null guard for this --
Postgres ``greatest(NULL, 0)`` returns ``0``, not ``NULL``, which would fabricate a zero-import
hour wherever telemetry gapped.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CounterfactualFlows:
    """The grid flows of one hour of a PV-only household, in kWh."""

    grid_import_kwh: float
    grid_export_kwh: float


def no_battery_flows(
    pv_production_kwh: float | None, household_load_kwh: float | None
) -> CounterfactualFlows | None:
    """Derive the PV-only grid flows for one hour, or ``None`` if either input is missing."""
    if pv_production_kwh is None or household_load_kwh is None:
        return None
    return CounterfactualFlows(
        grid_import_kwh=max(household_load_kwh - pv_production_kwh, 0.0),
        grid_export_kwh=max(pv_production_kwh - household_load_kwh, 0.0),
    )


def self_consumed_kwh(
    household_load_kwh: float | None, grid_import_kwh: float | None
) -> float | None:
    """Load served without the grid in one hour: ``load - import``.

    The textbook alternative, ``pv - export``, is equivalent whenever storage round-trips within
    the accounting period, and is what datasheets quote. It is deliberately not used, because
    the synthetic generator simulates each Berlin day independently -- that is what makes it a
    pure function of ``(config, date)`` -- so state of charge resets at midnight and whatever the
    battery still held is discarded. ``pv - export`` would count that discarded energy as
    self-consumed; ``load - import`` cannot, because it only counts energy that reached the load.

    It is also the quantity the economics want: ``load - import`` is exactly the import that did
    not happen, which is what avoided grid cost prices. All of it is PV-origin because the naive
    controller never charges from the grid; a grid-charging controller would need this revisited.
    """
    if household_load_kwh is None or grid_import_kwh is None:
        return None
    return household_load_kwh - grid_import_kwh
