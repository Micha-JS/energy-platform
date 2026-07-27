"""The MILP argument, demonstrated rather than asserted.

This milestone turns on enforcing both exclusivities with binary variables instead of checking them
afterwards. A claim like that is worth what its evidence is worth, so these tests solve the *same
window twice* -- once as the naive LP, once as the MILP -- and show what each returns.

Investigating it moved the argument somewhere more interesting than where it started.

**The meter is the live trap, and it is worse than simultaneous charge/discharge.** Feed-in is a
*flat* statutory rate that does not track the market. So the moment the retail import price falls
below that flat rate, importing and exporting the same kWh is paid at both ends, and nothing in a
pure LP stops the loop: the program is **unbounded**. No battery is involved, so no amount of
battery-side post-checking would ever have caught it -- and there is no optimum to "repair" towards.
With this catalogue the crossover is at **spot = -123.75 EUR/MWh**, computed below from the seed
rather than pinned, so a rate change moves the test with it.

**The battery binary is a guarantee, not a fix for a wrong answer.** Once the meter is exclusive,
charging and discharging in the same hour turns out to be exactly *objective-neutral*: in import
mode the metered flow is ``load + c - pv - d``, in export mode ``pv + d - load - c``, so raising
both legs together moves the meter not at all while strictly costing state of charge through the
round-trip legs. It is weakly dominated and never strictly optimal, and no price in the declared
range makes it pay -- which is why no test here can produce it as an LP counterexample, and saying
so is more useful than implying otherwise. What remains is degeneracy: it is reachable as a tie,
and which vertex a simplex returns among equally-optimal ones is not something the model gets to
promise. The binary turns "the solver happens not to" into "the formulation does not permit it",
for ~168 extra binaries that HiGHS closes in milliseconds.

Neither pathology fires on the shipped demo data, whose seeded windows bottom out at -59.92
EUR/MWh -- which is exactly why these fixtures are constructed. Both thresholds sit inside the
-500..4000 EUR/MWh range the warehouse declares acceptable and the M5 property tests generate over,
and German day-ahead hours have gone below -123 EUR/MWh in living memory. A guard that only holds
for the data we happen to ship is not a guard.

Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import pytest

from energy_platform.config import BatteryConfig
from energy_platform.dispatch.model import HourInputs
from energy_platform.dispatch.optimizer import solve_raw, solve_window
from energy_platform.dispatch.pricing import WindowPrices, window_prices
from energy_platform.tariffs.catalog import load_catalog
from energy_platform.tariffs.engine import import_price_ct_kwh
from tests.dispatch.conftest import make_hours

# The real catalogue, not a synthetic one: the thresholds below are properties of the rates this
# repo actually ships, and a test that invented its own would not be about them.
_CATALOG = load_catalog()
DYNAMIC = _CATALOG["dynamic_2024"]
FEED_IN = _CATALOG["eeg_2024"]

BATTERY = BatteryConfig(
    capacity_kwh=14.0,
    soc_min=0.05,
    soc_max=1.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    round_trip_efficiency=0.90,
)

# The cheapest hour of the seeded March window. Quoted so that if a re-recorded fixture ever dips
# past a threshold, the claim "latent in the demo data" is checked rather than remembered.
SEEDED_MINIMUM_SPOT = -59.92


def meter_crossover_spot() -> float:
    """The spot price at which the retail import price equals the flat feed-in rate.

    Derived from the catalogue rather than hardcoded, by inverting the engine's own stack:
    ``(spot/10 + margin + pass_through) * (1 + vat) == feed_in``. Below this, buying and selling the
    same kWh in the same hour is paid at both ends.
    """
    assert DYNAMIC.margin_ct_kwh is not None
    assert DYNAMIC.pass_through_ct_kwh is not None
    assert FEED_IN.price_ct_kwh is not None
    net_ct_kwh = FEED_IN.price_ct_kwh / (1.0 + DYNAMIC.vat_rate)
    return (net_ct_kwh - DYNAMIC.margin_ct_kwh - DYNAMIC.pass_through_ct_kwh) * 10.0


def _window(spot: float, hours: int = 6) -> tuple[WindowPrices, tuple[HourInputs, ...]]:
    """A flat window at one spot price: no PV, 1 kWh of load an hour, the real catalogue."""
    prices: list[float | None] = [spot] * hours
    inputs = make_hours(prices, pv_kwh=[0.0] * hours, load_kwh=[1.0] * hours)
    return window_prices(DYNAMIC, FEED_IN, inputs), inputs


# -- The thresholds are real properties of the shipped catalogue -----------------------------


def test_the_meter_crossover_is_where_importing_pays_more_than_exporting() -> None:
    """Pins the arithmetic the module docstring quotes: about -123.75 EUR/MWh."""
    crossover = meter_crossover_spot()
    assert crossover == pytest.approx(-123.75, abs=0.01)

    at_crossover = import_price_ct_kwh(DYNAMIC, crossover)
    assert at_crossover == pytest.approx(FEED_IN.price_ct_kwh)

    above = import_price_ct_kwh(DYNAMIC, crossover + 5.0)
    below = import_price_ct_kwh(DYNAMIC, crossover - 5.0)
    assert above is not None and below is not None
    assert FEED_IN.price_ct_kwh is not None
    assert below < FEED_IN.price_ct_kwh < above


def test_the_retail_price_only_goes_negative_far_below_zero_spot() -> None:
    """A second, deeper threshold: -19.19 ct of margin and pass-through, grossed up by VAT."""
    above = import_price_ct_kwh(DYNAMIC, -191.0)
    below = import_price_ct_kwh(DYNAMIC, -193.0)
    assert above is not None and below is not None
    assert above > 0.0 > below


def test_the_seeded_demo_data_never_reaches_either_threshold() -> None:
    """Why these fixtures are constructed: the shipped data never exercises the pathology.

    The March window's cheapest hour leaves the retail price at a comfortable 15.7 ct/kWh, well
    above the flat feed-in rate. The guard exists for the declared range, not the observed one.
    """
    assert meter_crossover_spot() < SEEDED_MINIMUM_SPOT
    price = import_price_ct_kwh(DYNAMIC, SEEDED_MINIMUM_SPOT)
    assert FEED_IN.price_ct_kwh is not None
    assert price is not None and price > FEED_IN.price_ct_kwh


# -- The naive LP is unbounded past the crossover; the MILP is not ---------------------------


def test_the_naive_lp_is_bounded_while_importing_costs_more_than_exporting_earns() -> None:
    """Above the crossover there is no money pump, so the relaxation is well posed.

    Establishing this first is what makes the next test evidence rather than coincidence: the LP
    does not fail everywhere, it fails exactly where the rates say it should.
    """
    prices, hours = _window(meter_crossover_spot() + 5.0)
    relaxed = solve_raw(
        hours,
        prices,
        BATTERY,
        0.0,
        enforce_battery_exclusivity=False,
        enforce_meter_exclusivity=False,
    )
    assert relaxed.status == "Optimal"


def test_the_naive_lp_is_unbounded_below_the_crossover() -> None:
    """The failure the binaries exist for, and it has no finite answer to be repaired towards."""
    prices, hours = _window(meter_crossover_spot() - 5.0)
    relaxed = solve_raw(
        hours,
        prices,
        BATTERY,
        0.0,
        enforce_battery_exclusivity=False,
        enforce_meter_exclusivity=False,
    )
    assert relaxed.status == "Unbounded", (
        f"expected the naive LP to be unbounded just below the {meter_crossover_spot():.2f} "
        f"EUR/MWh crossover, got {relaxed.status!r} -- if this stops reproducing, the argument in "
        "the optimizer's docstring needs revisiting before the binaries are trusted"
    )


@pytest.mark.parametrize("spot", [-128.75, -200.0, -300.0, -500.0])
def test_the_milp_returns_a_proven_optimum_at_every_depth(spot: float) -> None:
    """Same windows, binaries on: a finite answer across the whole declared price range."""
    prices, hours = _window(spot)
    assert solve_raw(hours, prices, BATTERY, 0.0).status == "Optimal"


@pytest.mark.parametrize("spot", [-128.75, -300.0, -500.0])
def test_the_settled_milp_schedule_runs_the_meter_one_way_each_hour(spot: float) -> None:
    """And the persisted result inherits it, so no downstream reader sees an impossible hour."""
    prices, hours = _window(spot)
    result = solve_window(hours, prices, BATTERY, 0.0)

    assert result.status == "Optimal"
    for hour in result.hours:
        assert hour.grid_import_kwh is not None
        assert hour.grid_export_kwh is not None
        assert min(hour.grid_import_kwh, hour.grid_export_kwh) == 0.0
        assert hour.battery_charge_kwh is not None
        assert hour.battery_discharge_kwh is not None
        assert min(hour.battery_charge_kwh, hour.battery_discharge_kwh) == 0.0


# -- The battery binary: dominated, not merely unprofitable ----------------------------------


def test_dropping_the_battery_binary_alone_changes_nothing_about_the_optimum() -> None:
    """Evidence for the honest version of the claim, rather than the convenient one.

    With the meter exclusive, raising charge and discharge together moves the metered flow not at
    all and strictly costs state of charge, so it is weakly dominated at *every* price. The two
    programs therefore agree on the objective -- which is why the binary is justified as a
    guarantee against degenerate ties, not as a fix for a wrong answer the LP would otherwise give.
    """
    for spot in (-300.0, -128.75, 0.0, 120.0):
        prices, hours = _window(spot)
        with_binary = solve_raw(hours, prices, BATTERY, 0.0)
        without = solve_raw(hours, prices, BATTERY, 0.0, enforce_battery_exclusivity=False)
        assert with_binary.status == without.status == "Optimal"
        assert with_binary.objective is not None and without.objective is not None
        assert with_binary.objective == pytest.approx(without.objective, abs=1e-6), (
            f"at {spot} EUR/MWh the battery binary changed the optimal value, so it is doing more "
            "than breaking ties and the docstring's reasoning is wrong"
        )


# -- Signs survive all the way to the settled total ------------------------------------------


def test_being_paid_to_import_is_priced_as_a_negative_cost_not_clamped() -> None:
    """M5's contract, carried into M6.

    A "costs cannot be negative" clamp anywhere in the stack would silently destroy the very hours
    a dynamic tariff exists to exploit.
    """
    prices, hours = _window(-400.0)
    result = solve_window(hours, prices, BATTERY, 0.0)

    assert result.energy_cost_eur < 0.0
    assert all(
        hour.import_price_ct_kwh is not None and hour.import_price_ct_kwh < 0.0
        for hour in result.hours
    )


def test_feed_in_revenue_stays_positive_however_negative_the_spot_goes() -> None:
    """Feed-in is a flat statutory rate; pricing exports off the spot would invert the sign."""
    prices, _ = _window(-500.0)
    assert FEED_IN.price_ct_kwh is not None
    assert prices.feed_in_eur_kwh == pytest.approx(FEED_IN.price_ct_kwh / 100.0)
    assert prices.feed_in_eur_kwh > 0.0
