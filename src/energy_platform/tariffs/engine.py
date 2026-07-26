"""Tariff arithmetic: what an hour of imported or exported energy is worth.

Every function here is **null-propagating**: a missing input yields ``None``, never a
fabricated zero. That is the same NaN-over-fabrication invariant the raw zone and the dbt
models hold, carried into the pricing layer -- an hour whose telemetry gapped has no cost, and
that is different from an hour that cost nothing.

**Units.** The warehouse stores wholesale price in EUR/MWh and energy in kWh; retail tariffs
are quoted in ct/kWh. The conversion DIVIDES by **10**, not 1000::

    100 EUR/MWh  ==  10 ct/kWh  ==  0.10 EUR/kWh

Getting that wrong is silent -- the numbers stay plausible, just off by 10x -- so the
conversion is a named constant here, mirrored in the ``tariff_import_price_ct_kwh`` dbt macro,
and asserted explicitly on both sides.

**VAT.** Every per-kWh parameter in the catalogue is net. ``vat_rate`` is applied exactly once,
here, to the consumption side: energy cost and the standing charge. Feed-in compensation is
outside VAT (Kleinunternehmerregelung), so :func:`feed_in_revenue_eur` never grosses up.

**Negative prices.** Day-ahead prices go negative, and nothing here clamps them. A dynamic
import price can legitimately fall below zero, which means the household is paid to consume;
that is arithmetic, not an error. Feed-in compensation is a flat statutory rate and is
therefore *independent* of the spot price -- it cannot go negative, and computing it as
``spot x export`` would be wrong.
"""

from __future__ import annotations

from typing import Final

from energy_platform.tariffs.catalog import TariffKind, TariffSpec

# EUR/MWh per ct/kWh. One EUR is 100 ct and one MWh is 1000 kWh, so ten EUR/MWh make one ct/kWh:
# the conversion DIVIDES by 10. Worked example: 100 EUR/MWh / 10 == 10 ct/kWh == 0.10 EUR/kWh.
#
# A divisor rather than a 0.1 multiplier on purpose: 0.1 has no exact binary representation, so
# multiplying injects rounding noise into every price (-12.863900000000003 where -12.8639 was
# meant). Dividing by an exact integer keeps the result exact at these magnitudes on both sides of
# the Python/SQL boundary, which is what lets the reconciliation test compare tightly. Mirrors the
# eur_mwh_per_ct_kwh dbt macro.
EUR_PER_MWH_PER_CT_PER_KWH: Final = 10.0

# ct -> EUR at the single point where a per-kWh rate becomes money.
CT_PER_EUR: Final = 100.0


def spot_ct_kwh(price_eur_mwh: float | None) -> float | None:
    """Convert a wholesale day-ahead price from EUR/MWh to ct/kWh. Sign is preserved."""
    if price_eur_mwh is None:
        return None
    return price_eur_mwh / EUR_PER_MWH_PER_CT_PER_KWH


def import_price_ct_kwh(spec: TariffSpec, price_eur_mwh: float | None = None) -> float | None:
    """The gross retail price of one imported kWh under ``spec``, in ct/kWh.

    A static tariff ignores ``price_eur_mwh`` entirely. A dynamic tariff stacks the hourly spot
    price, the supplier margin, and the fixed pass-through components (grid fees, levies,
    electricity tax) -- all net -- and grosses the total up by VAT once. With a null spot price
    a dynamic tariff has no price for that hour, so the result is ``None``.
    """
    if not spec.is_consumption:
        raise ValueError(
            f"tariff {spec.tariff_id!r} is a {spec.kind.value} row and prices exports, not "
            "imports; use feed_in_revenue_eur"
        )

    if spec.kind is TariffKind.STATIC:
        assert spec.price_ct_kwh is not None  # guaranteed by TariffSpec.__post_init__
        net = spec.price_ct_kwh
    else:
        spot = spot_ct_kwh(price_eur_mwh)
        if spot is None:
            return None
        assert spec.margin_ct_kwh is not None  # guaranteed by TariffSpec.__post_init__
        assert spec.pass_through_ct_kwh is not None
        net = spot + spec.margin_ct_kwh + spec.pass_through_ct_kwh

    return net * (1.0 + spec.vat_rate)


def energy_cost_eur(
    spec: TariffSpec,
    grid_import_kwh: float | None,
    price_eur_mwh: float | None = None,
) -> float | None:
    """Cost in EUR of the energy imported in one hour. ``None`` if any input is missing."""
    if grid_import_kwh is None:
        return None
    price = import_price_ct_kwh(spec, price_eur_mwh)
    if price is None:
        return None
    return grid_import_kwh * price / CT_PER_EUR


def feed_in_revenue_eur(spec: TariffSpec, grid_export_kwh: float | None) -> float | None:
    """Compensation in EUR for the energy exported in one hour.

    A flat statutory rate per kWh, deliberately taking no price argument: feed-in compensation
    does not track the wholesale market, so a negative spot hour still earns the full rate.
    """
    if spec.kind is not TariffKind.FEED_IN:
        raise ValueError(
            f"tariff {spec.tariff_id!r} is a {spec.kind.value} row and prices imports, not "
            "exports; use energy_cost_eur"
        )
    if grid_export_kwh is None:
        return None
    assert spec.price_ct_kwh is not None  # guaranteed by TariffSpec.__post_init__
    return grid_export_kwh * spec.price_ct_kwh / CT_PER_EUR


def base_fee_eur(spec: TariffSpec, priced_hours: int, expected_hours: int) -> float | None:
    """The standing charge for a period, pro-rated by how much of the month is actually priced.

    The seeded demo windows are a week long, so charging a whole month's Grundpreis against
    seven days of energy would make ``total_cost_eur`` incomparable between months. Pro-rating
    by ``priced_hours / expected_hours`` keeps the total meaningful; the full monthly fee and
    both hour counts stay exposed alongside it so a consumer can undo this.

    ``expected_hours`` is the DST-correct length of the calendar month (23- and 25-hour days
    included), derived from the calendar rather than counted from the rows.
    """
    if spec.base_fee_eur_month is None:
        return None
    if expected_hours <= 0:
        raise ValueError(
            f"expected_hours must be positive, got {expected_hours} -- it is the calendar "
            "length of the month, never a count of the rows under test"
        )
    return spec.base_fee_eur_month * (1.0 + spec.vat_rate) * priced_hours / expected_hours
