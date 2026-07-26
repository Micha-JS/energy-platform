"""Property tests for the two invariants the economics rest on.

The example-based tests next door pin specific values; these assert the properties that must
hold for *every* hour, including the ones nobody thought to write down -- negative prices, zero
generation, a load of nothing, VAT of nothing. Hypothesis generates the inputs.

* **Cost decomposition:** a dynamic tariff's cost is exactly (spot + margin + pass-through)
  grossed up by VAT, times the imported energy -- for any sign of spot.
* **Counterfactual conservation:** the no-battery flows satisfy pv + import == load + export
  exactly, and never import and export in the same hour.

Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from energy_platform.tariffs.catalog import TariffKind, TariffSpec
from energy_platform.tariffs.counterfactual import no_battery_flows, self_consumed_kwh
from energy_platform.tariffs.engine import energy_cost_eur, feed_in_revenue_eur

# Day-ahead prices span roughly -500..4000 EUR/MWh in the accepted range the dbt tests declare;
# the negative tail is the interesting half and is deliberately well represented here.
_spot_prices = st.floats(-500.0, 4000.0)
_energy_kwh = st.floats(0.0, 20.0)

_dynamic_tariffs = st.builds(
    TariffSpec,
    tariff_id=st.just("dynamic"),
    label=st.just(""),
    kind=st.just(TariffKind.DYNAMIC),
    margin_ct_kwh=st.floats(0.0, 10.0),
    pass_through_ct_kwh=st.floats(0.0, 40.0),
    base_fee_eur_month=st.floats(0.0, 30.0),
    vat_rate=st.floats(0.0, 0.5),
)

_feed_in_tariffs = st.builds(
    TariffSpec,
    tariff_id=st.just("feed_in"),
    label=st.just(""),
    kind=st.just(TariffKind.FEED_IN),
    price_ct_kwh=st.floats(0.0, 30.0),
)


# -- Property: the dynamic cost decomposes exactly ------------------------------------------


@given(spec=_dynamic_tariffs, spot=_spot_prices, grid_import_kwh=_energy_kwh)
def test_dynamic_cost_is_price_times_import_including_fees(
    spec: TariffSpec, spot: float, grid_import_kwh: float
) -> None:
    """For any hour: cost == (spot/10 + margin + pass_through) * (1 + vat) * import / 100.

    Written out longhand rather than by calling the engine, so this is an independent statement
    of the formula and not a restatement of the implementation.
    """
    assert spec.margin_ct_kwh is not None
    assert spec.pass_through_ct_kwh is not None
    expected_ct_kwh = (spot / 10.0 + spec.margin_ct_kwh + spec.pass_through_ct_kwh) * (
        1.0 + spec.vat_rate
    )
    expected_eur = grid_import_kwh * expected_ct_kwh / 100.0

    cost = energy_cost_eur(spec, grid_import_kwh, spot)
    # Relative tolerance: the products span several orders of magnitude across the input range.
    assert cost == pytest.approx(expected_eur, rel=1e-12, abs=1e-15)


@given(spec=_dynamic_tariffs, spot=_spot_prices)
def test_cost_scales_linearly_with_imported_energy(spec: TariffSpec, spot: float) -> None:
    """Doubling the import doubles the cost -- there is no per-hour fixed term hiding in there."""
    one = energy_cost_eur(spec, 1.0, spot)
    two = energy_cost_eur(spec, 2.0, spot)
    assert one is not None and two is not None
    assert two == pytest.approx(2.0 * one, rel=1e-12, abs=1e-15)


@given(spec=_dynamic_tariffs, spot=_spot_prices)
def test_importing_nothing_costs_nothing_whatever_the_price(spec: TariffSpec, spot: float) -> None:
    """Even at a negative price: no energy crossed the meter, so no money did either."""
    assert energy_cost_eur(spec, 0.0, spot) == pytest.approx(0.0)


@given(spec=_feed_in_tariffs, export=_energy_kwh, spot=_spot_prices)
def test_feed_in_revenue_ignores_the_spot_price_entirely(
    spec: TariffSpec, export: float, spot: float
) -> None:
    """The flat rate does not track the market, so revenue is never negative -- the sign trap."""
    revenue = feed_in_revenue_eur(spec, export)
    assert revenue is not None and revenue >= 0.0
    assert revenue == pytest.approx(export * (spec.price_ct_kwh or 0.0) / 100.0)


# -- Property: the no-battery counterfactual conserves energy -------------------------------


@given(pv=_energy_kwh, load=_energy_kwh)
def test_no_battery_counterfactual_conserves_energy(pv: float, load: float) -> None:
    """pv + import == load + export, exactly: with no storage there is nowhere for energy to go."""
    flows = no_battery_flows(pv, load)
    assert flows is not None
    # Exact identity, not an approximation -- the tolerance only budgets the float addition.
    assert pv + flows.grid_import_kwh == pytest.approx(
        load + flows.grid_export_kwh, rel=1e-12, abs=1e-12
    )


@given(pv=_energy_kwh, load=_energy_kwh)
def test_counterfactual_flows_are_non_negative_and_mutually_exclusive(
    pv: float, load: float
) -> None:
    """An hour imports or exports, never both, and never a negative amount of either."""
    flows = no_battery_flows(pv, load)
    assert flows is not None
    assert flows.grid_import_kwh >= 0.0
    assert flows.grid_export_kwh >= 0.0
    assert min(flows.grid_import_kwh, flows.grid_export_kwh) == 0.0


@given(pv=_energy_kwh, load=_energy_kwh)
def test_counterfactual_self_consumption_never_exceeds_generation_or_load(
    pv: float, load: float
) -> None:
    """Self-consumption is bounded by both what was produced and what was needed."""
    flows = no_battery_flows(pv, load)
    assert flows is not None
    consumed = self_consumed_kwh(load, flows.grid_import_kwh)
    assert consumed is not None
    assert -1e-12 <= consumed <= min(pv, load) + 1e-12


@given(
    pv=st.one_of(st.none(), _energy_kwh),
    load=st.one_of(st.none(), _energy_kwh),
)
def test_a_missing_input_yields_no_counterfactual_at_all(
    pv: float | None, load: float | None
) -> None:
    """NaN over fabrication: an unknown hour stays unknown rather than collapsing to zero flows."""
    flows = no_battery_flows(pv, load)
    if pv is None or load is None:
        assert flows is None
    else:
        assert flows is not None
