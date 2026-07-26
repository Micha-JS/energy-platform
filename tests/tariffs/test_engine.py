"""Tariff arithmetic: unit conversion, VAT, sign handling, and null propagation.

These pin the three things that go wrong silently when pricing energy: a factor-of-ten unit
slip between EUR/MWh and ct/kWh, VAT applied twice or not at all, and a negative day-ahead
price clamped into nonsense. Each is asserted on its own so a failure names the mistake. The
SQL twin of this arithmetic is pinned separately by tests/dbt/test_tariff_reconciliation.py.
Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import pytest

from energy_platform.tariffs.catalog import TariffKind, TariffSpec
from energy_platform.tariffs.engine import (
    base_fee_eur,
    energy_cost_eur,
    feed_in_revenue_eur,
    import_price_ct_kwh,
    spot_ct_kwh,
)

# A tariff stripped to nothing but the spot price, so an assertion about the unit conversion is
# about the unit conversion and not about the margin stack on top of it.
BARE_DYNAMIC = TariffSpec(
    tariff_id="bare_dynamic",
    label="spot only, no margin, no VAT",
    kind=TariffKind.DYNAMIC,
    margin_ct_kwh=0.0,
    pass_through_ct_kwh=0.0,
    base_fee_eur_month=0.0,
    vat_rate=0.0,
)

# The shipped catalogue values, so the tests exercise the real numbers as well as clean ones.
STATIC = TariffSpec(
    tariff_id="static_2024",
    label="Static household tariff (2024)",
    kind=TariffKind.STATIC,
    price_ct_kwh=29.41,
    base_fee_eur_month=10.84,
    vat_rate=0.19,
)
DYNAMIC = TariffSpec(
    tariff_id="dynamic_2024",
    label="Dynamic day-ahead tariff (2024)",
    kind=TariffKind.DYNAMIC,
    margin_ct_kwh=1.79,
    pass_through_ct_kwh=17.40,
    base_fee_eur_month=10.84,
    vat_rate=0.19,
)
FEED_IN = TariffSpec(
    tariff_id="eeg_2024",
    label="EEG feed-in for surplus on systems <=10 kWp (2024)",
    kind=TariffKind.FEED_IN,
    price_ct_kwh=8.11,
)


# -- Unit conversion: the classic silent factor of ten --------------------------------------


@pytest.mark.parametrize(
    ("price_eur_mwh", "expected_ct_kwh"),
    [
        (100.0, 10.0),  # the worked example: 100 EUR/MWh == 10 ct/kWh == 0.10 EUR/kWh
        (0.0, 0.0),
        (-80.0, -8.0),  # sign survives the conversion
        (1.0, 0.1),
    ],
)
def test_wholesale_price_converts_by_ten_not_a_thousand(
    price_eur_mwh: float, expected_ct_kwh: float
) -> None:
    """EUR/MWh -> ct/kWh divides by 10. A /1000 leaves every downstream total 10x too small."""
    assert spot_ct_kwh(price_eur_mwh) == pytest.approx(expected_ct_kwh, abs=1e-12)


def test_one_kwh_at_one_hundred_eur_per_mwh_costs_ten_cents() -> None:
    """The conversion end to end, in money: the number a bill would show."""
    assert import_price_ct_kwh(BARE_DYNAMIC, 100.0) == pytest.approx(10.0)
    assert energy_cost_eur(BARE_DYNAMIC, 1.0, 100.0) == pytest.approx(0.10)


# -- VAT: applied exactly once, and only to the consumption side ----------------------------


def test_static_price_is_the_net_rate_grossed_up_once() -> None:
    assert import_price_ct_kwh(STATIC) == pytest.approx(29.41 * 1.19)


def test_dynamic_price_is_spot_plus_margin_plus_pass_through_then_vat() -> None:
    """VAT applies to the whole stack, not to the margin alone -- (a+b+c)*v, never a+b+c*v."""
    assert import_price_ct_kwh(DYNAMIC, 200.0) == pytest.approx((20.0 + 1.79 + 17.40) * 1.19)


def test_feed_in_compensation_is_outside_vat() -> None:
    """A private surplus feed-in earns the flat statutory rate; no VAT is added to it."""
    assert feed_in_revenue_eur(FEED_IN, 1.0) == pytest.approx(0.0811)


def test_zero_vat_leaves_the_net_price_untouched() -> None:
    """Guards against a stray (1 + vat) that would still be wrong at vat = 0 if written as *vat."""
    assert import_price_ct_kwh(BARE_DYNAMIC, 500.0) == pytest.approx(50.0)


# -- Negative day-ahead prices: sign must survive, on both sides of the meter ----------------


def test_negative_spot_lowers_the_dynamic_price_without_clamping() -> None:
    assert import_price_ct_kwh(DYNAMIC, -80.0) == pytest.approx((-8.0 + 1.79 + 17.40) * 1.19)


def test_deeply_negative_spot_makes_importing_earn_money() -> None:
    """Below about -192 EUR/MWh this tariff's price passes zero: consuming pays. Not an error."""
    price = import_price_ct_kwh(DYNAMIC, -300.0)
    assert price is not None and price < 0
    cost = energy_cost_eur(DYNAMIC, 2.0, -300.0)
    assert cost is not None and cost < 0


def test_negative_spot_does_not_touch_the_static_price() -> None:
    assert import_price_ct_kwh(STATIC, -300.0) == import_price_ct_kwh(STATIC, 300.0)


def test_negative_spot_does_not_touch_feed_in_revenue() -> None:
    """The trap: feed-in is a flat rate, so computing it as spot x export would invert the sign."""
    assert feed_in_revenue_eur(FEED_IN, 5.0) == pytest.approx(5.0 * 0.0811)


# -- Null propagation: a gap has no cost, which is not a cost of zero ------------------------


def test_dynamic_tariff_has_no_price_without_a_spot_price() -> None:
    assert import_price_ct_kwh(DYNAMIC, None) is None
    assert energy_cost_eur(DYNAMIC, 1.0, None) is None


def test_static_tariff_prices_an_hour_the_market_did_not() -> None:
    """A static price does not depend on the spot, so a missing spot must not null it out."""
    assert energy_cost_eur(STATIC, 1.0, None) == pytest.approx(0.2941 * 1.19)


def test_missing_energy_yields_no_cost_and_no_revenue() -> None:
    assert energy_cost_eur(STATIC, None) is None
    assert energy_cost_eur(DYNAMIC, None, 100.0) is None
    assert feed_in_revenue_eur(FEED_IN, None) is None


# -- Using a tariff for the wrong side of the meter is an error, not a silent zero -----------


def test_pricing_imports_with_a_feed_in_row_raises() -> None:
    with pytest.raises(ValueError, match="prices exports"):
        import_price_ct_kwh(FEED_IN, 100.0)


def test_pricing_exports_with_a_consumption_row_raises() -> None:
    with pytest.raises(ValueError, match="prices imports"):
        feed_in_revenue_eur(STATIC, 1.0)


# -- The standing charge, pro-rated by coverage ---------------------------------------------


def test_base_fee_is_prorated_by_priced_hours() -> None:
    """A week of a 720-hour month carries a week of the Grundpreis, grossed up by VAT."""
    assert base_fee_eur(STATIC, 168, 720) == pytest.approx(10.84 * 1.19 * 168 / 720)


def test_a_complete_month_carries_the_whole_base_fee() -> None:
    assert base_fee_eur(STATIC, 720, 720) == pytest.approx(10.84 * 1.19)


def test_a_month_with_nothing_priced_carries_no_base_fee() -> None:
    assert base_fee_eur(STATIC, 0, 744) == pytest.approx(0.0)


def test_feed_in_rows_have_no_standing_charge() -> None:
    assert base_fee_eur(FEED_IN, 168, 720) is None


def test_zero_expected_hours_raises_rather_than_dividing() -> None:
    """expected_hours comes from the calendar; a zero means the caller counted rows instead."""
    with pytest.raises(ValueError, match="expected_hours must be positive"):
        base_fee_eur(STATIC, 10, 0)
