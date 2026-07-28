"""Synthetic telemetry: determinism, the PV model, and the energy-conservation invariants.

The pure-logic tests pin the data contract (seed recipe, single-boundary rounding, null
propagation); the Hypothesis property tests assert the two load-bearing invariants -- AC-node
conservation every hour and SoC continuity/bounds -- that make the naive baseline trustworthy
for the M6 comparison. Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import math
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from energy_platform.config import (
    BatteryConfig,
    PvSystemConfig,
    SyntheticConfig,
    ThermalConfig,
)
from energy_platform.connectors.synthetic import (
    TELEMETRY_DATASETS,
    SyntheticTelemetryClient,
    SyntheticTelemetryError,
    dispatch_hour,
    emit,
    initial_indoor_temp_c,
    pv_production_kwh,
    seed_for,
    simulate_day,
    thermal_step,
    thermostat_step,
    utc_hour_instants,
)
from energy_platform.connectors.types import Dataset, Point, Resolution
from energy_platform.orchestration.ingest import berlin_day_window, content_hash

PV = PvSystemConfig()
BATTERY = BatteryConfig()
SYNTHETIC = SyntheticConfig()
THERMAL = ThermalConfig()

# A clear-ish summer day (no DST transition -> a clean 24-hour window).
DAY = date(2024, 6, 15)
WINDOW = berlin_day_window(DAY)


def _bell_ghi(peak: float = 850.0) -> dict[int, float | None]:
    """A plausible single-humped daily irradiance curve over the window's hours (W/m^2)."""
    instants = utc_hour_instants(WINDOW)
    n = len(instants)
    ghi: dict[int, float | None] = {}
    for i, ts in enumerate(instants):
        # Zero overnight, a smooth hump around local midday.
        x = (i - n / 2) / (n / 5)
        ghi[ts] = max(0.0, peak * math.exp(-x * x))
    return ghi


def _flat_temp(value: float = 18.0) -> dict[int, float | None]:
    return {ts: value for ts in utc_hour_instants(WINDOW)}


# -- Data contract: seed, emission boundary, null propagation --------------------------


def test_seed_is_deterministic_and_date_specific() -> None:
    assert seed_for("salt", DAY) == seed_for("salt", DAY)
    assert seed_for("salt", DAY) != seed_for("salt", date(2024, 6, 16))
    assert seed_for("salt-a", DAY) != seed_for("salt-b", DAY)


def test_seed_recipe_is_pinned() -> None:
    # The exact recipe is part of the data contract: sha256("salt:isodate")[:8] big-endian.
    import hashlib

    expected = int.from_bytes(hashlib.sha256(b"s:2024-06-15").digest()[:8], "big")
    assert seed_for("s", DAY) == expected


def test_emit_quantises_to_wh_and_normalises_zero() -> None:
    assert emit(1.23456) == 1.235
    assert emit(None) is None
    negative_zero = emit(-0.0)
    assert negative_zero == 0.0
    assert negative_zero is not None
    assert math.copysign(1.0, negative_zero) == 1.0  # positive zero, not -0.0


def test_simulate_day_is_deterministic() -> None:
    ghi, temp = _bell_ghi(), _flat_temp()
    first = simulate_day(DAY, WINDOW, ghi, temp, PV, BATTERY, SYNTHETIC, THERMAL)
    second = simulate_day(DAY, WINDOW, ghi, temp, PV, BATTERY, SYNTHETIC, THERMAL)
    assert first == second
    # Determinism is what makes re-ingestion a no-op: identical points -> identical hash.
    for dataset, points in first.items():
        assert content_hash(dataset, "home", Resolution.HOUR, DAY, points) == content_hash(
            dataset, "home", Resolution.HOUR, DAY, second[dataset]
        )


def test_every_series_has_one_point_per_hour() -> None:
    series = simulate_day(DAY, WINDOW, _bell_ghi(), _flat_temp(), PV, BATTERY, SYNTHETIC, THERMAL)
    assert set(series) == set(TELEMETRY_DATASETS)
    for points in series.values():
        assert len(points) == WINDOW.expected_count(Resolution.HOUR)


def test_missing_irradiance_nulls_every_series_for_that_hour() -> None:
    ghi = _bell_ghi()
    instants = utc_hour_instants(WINDOW)
    gap = instants[12]
    ghi[gap] = None  # midday hole
    series = simulate_day(DAY, WINDOW, ghi, _flat_temp(), PV, BATTERY, SYNTHETIC, THERMAL)
    for dataset in TELEMETRY_DATASETS:
        by_ts = dict(series[dataset])
        assert by_ts[gap] is None, dataset
        assert by_ts[instants[11]] is not None, dataset  # neighbours still simulated


def test_absent_irradiance_row_also_nulls_the_hour() -> None:
    # A missing key (no row at all), not just a null value, is handled identically.
    ghi = _bell_ghi()
    instants = utc_hour_instants(WINDOW)
    del ghi[instants[10]]
    series = simulate_day(DAY, WINDOW, ghi, _flat_temp(), PV, BATTERY, SYNTHETIC, THERMAL)
    assert dict(series[Dataset.PV_PRODUCTION])[instants[10]] is None


# -- PV model --------------------------------------------------------------------------


def test_pv_zero_irradiance_is_zero() -> None:
    assert pv_production_kwh(0.0, 20.0, PV) == 0.0


def test_pv_clipped_at_ac_cap() -> None:
    capped = PvSystemConfig(dc_kwp=20.0, ac_cap_kw=5.0, performance_ratio=1.0)
    assert pv_production_kwh(1000.0, None, capped) == pytest.approx(5.0)


def test_missing_temperature_applies_no_derate() -> None:
    # Missing temp -> derate 1.0, which is >= the derate at a warmed 25 degC-reference cell.
    hot_ref = pv_production_kwh(800.0, 25.0, PV)
    no_temp = pv_production_kwh(800.0, None, PV)
    assert no_temp >= hot_ref
    assert no_temp == pytest.approx(PV.dc_kwp * 0.8 * PV.performance_ratio)


def test_pv_never_negative() -> None:
    # A physically absurd temperature drives the linear derate below zero; output floors at 0.
    assert pv_production_kwh(500.0, 400.0, PV) == 0.0


# -- The conditioned zone (M10) --------------------------------------------------------


def test_thermostat_latches_across_the_deadband() -> None:
    # Below the band: off, and stays off through the band on the way up.
    assert thermostat_step(20.0, False, THERMAL) is False
    assert thermostat_step(24.2, False, THERMAL) is False  # inside the band, not yet triggered
    # At the upper edge it fires, and then holds on all the way back down through the band...
    assert thermostat_step(24.5, False, THERMAL) is True
    assert thermostat_step(24.0, True, THERMAL) is True
    assert thermostat_step(23.6, True, THERMAL) is True
    # ...releasing only at the lower edge. Without this latch it is a comparator, not a thermostat.
    assert thermostat_step(23.5, True, THERMAL) is False


def test_ac_runs_when_hot_and_never_when_cold() -> None:
    hot = thermal_step(26.0, 30.0, 800.0, False, THERMAL)
    assert hot.ac_power_kwh == THERMAL.rated_kw
    assert hot.indoor_temp_c < 26.0  # cooling actually removes heat

    cold = thermal_step(21.0, 2.0, 0.0, False, THERMAL)
    assert cold.ac_power_kwh == 0.0


def test_heating_is_a_floor_and_never_an_electrical_load() -> None:
    # A freezing night pulls the zone down; the floor holds it, and draws no `ac_power` doing so,
    # because the house's heating is not electric. Modelling it as a load would put a phantom
    # winter consumer into every downstream cost.
    zone = thermal_step(20.5, -10.0, 0.0, False, THERMAL)
    assert zone.indoor_temp_c == THERMAL.heating_setpoint_c
    assert zone.ac_power_kwh == 0.0


def test_the_zone_is_deterministic_and_draws_no_randomness() -> None:
    # The RC model must not touch the RNG: the generator's stream is aligned one draw per hour to
    # the household-load profile, and an extra draw would silently rewrite every historical value.
    a = thermal_step(25.0, 28.0, 600.0, False, THERMAL)
    b = thermal_step(25.0, 28.0, 600.0, False, THERMAL)
    assert a == b


def test_initial_temperature_is_forgotten() -> None:
    """The daily reset's influence decays, and this is the number that says how fast.

    ``initial_indoor_temp_c`` picks the day's start from the day's own weather rather than from a
    constant, precisely so this bound can be tight. Two plausible starting states -- the
    controller's equilibrium and a zone 3 K warmer -- must agree well before the day is out; if a
    future change to R, C or the thermostat made the reset visible across the whole day, the
    series would carry a one-day sawtooth and this test is what notices.
    """
    outdoor, ghi = 26.0, 500.0
    settled = initial_indoor_temp_c(outdoor, ghi, THERMAL)

    def trajectory(start: float) -> list[float]:
        temp, on, out = start, False, []
        for _ in range(24):
            zone = thermal_step(temp, outdoor, ghi, on, THERMAL)
            temp, on = zone.indoor_temp_c, zone.compressor_on
            out.append(temp)
        return out

    disturbed = trajectory(settled + 3.0)
    baseline = trajectory(settled)
    # Within eight hours the 3 K disturbance is down to well under half a deadband.
    assert abs(disturbed[7] - baseline[7]) < THERMAL.deadband_k / 2


def test_the_daily_start_tracks_the_season_instead_of_a_constant() -> None:
    # Cold day -> the heating floor; hot day -> the cooling setpoint; mild day -> in between.
    # A constant start would put a cold March day 4 K above where the house actually sits and let
    # it decay all day, since nothing heats the zone back down within one R*C.
    assert initial_indoor_temp_c(2.0, 50.0, THERMAL) == THERMAL.heating_setpoint_c
    assert initial_indoor_temp_c(30.0, 700.0, THERMAL) == THERMAL.setpoint_c
    mild = initial_indoor_temp_c(21.0, 300.0, THERMAL)
    assert THERMAL.heating_setpoint_c <= mild <= THERMAL.setpoint_c


def test_load_base_is_bit_identical_to_the_pre_m10_household_load() -> None:
    """The split adds the AC on top; it does not redistribute what was already there.

    ``load_base`` must reproduce exactly what ``household_load`` used to be, which is only true if
    the thermal model draws no randomness -- one stray ``rng`` call would shift the whole stream
    and quietly rewrite the seeded history. Reconstructing the old series from the same public
    function the generator uses is the check.
    """
    import random

    from energy_platform.connectors.synthetic import household_load_kwh, quantise, seed_for

    series = simulate_day(DAY, WINDOW, _bell_ghi(), _flat_temp(), PV, BATTERY, SYNTHETIC, THERMAL)
    rng = random.Random(seed_for(SYNTHETIC.salt, DAY))
    expected = [
        quantise(household_load_kwh(ts, rng, SYNTHETIC)) for ts in utc_hour_instants(WINDOW)
    ]

    assert [value for _, value in series[Dataset.LOAD_BASE]] == expected


def test_missing_outdoor_temperature_nulls_the_hour() -> None:
    # Outdoor temperature became load-bearing at M10: it is the RC model's driving input, so an
    # hour without it cannot be simulated at all. Before M10 it was a second-order PV derate an
    # hour could do without, and `pv_production_kwh` still behaves that way as a pure function.
    temp = _flat_temp()
    instants = utc_hour_instants(WINDOW)
    gap = instants[13]
    temp[gap] = None
    series = simulate_day(DAY, WINDOW, _bell_ghi(), temp, PV, BATTERY, SYNTHETIC, THERMAL)
    for dataset in TELEMETRY_DATASETS:
        by_ts = dict(series[dataset])
        assert by_ts[gap] is None, dataset
        assert by_ts[instants[12]] is not None, dataset


def test_indoor_temperature_stays_habitable_across_the_year() -> None:
    # A cheap sanity net on the oracle M11 will learn from: whatever the weather, the modelled
    # house stays in a range a person would live in. A regression that inverted a sign or dropped
    # the heating floor would show up here long before it showed up as a strange optimiser.
    for outdoor in (-15.0, 0.0, 10.0, 20.0, 35.0):
        temp, on = THERMAL.setpoint_c, False
        for hour in range(72):
            ghi = 700.0 if hour % 24 in range(9, 17) else 0.0
            zone = thermal_step(temp, outdoor, ghi, on, THERMAL)
            temp, on = zone.indoor_temp_c, zone.compressor_on
            assert THERMAL.heating_setpoint_c - 0.01 <= temp <= 30.0, (outdoor, hour, temp)


# -- Property tests: the invariants ----------------------------------------------------

_batteries = st.builds(
    BatteryConfig,
    capacity_kwh=st.floats(5.0, 30.0),
    soc_min=st.floats(0.0, 0.2),
    soc_max=st.floats(0.8, 1.0),
    max_charge_kw=st.floats(1.0, 10.0),
    max_discharge_kw=st.floats(1.0, 10.0),
    round_trip_efficiency=st.floats(0.5, 1.0),
)


@given(
    pv=st.floats(0.0, 15.0),
    load=st.floats(0.0, 15.0),
    frac=st.floats(0.0, 1.0),
    battery=_batteries,
)
def test_dispatch_hour_invariants(
    pv: float, load: float, frac: float, battery: BatteryConfig
) -> None:
    cap = battery.capacity_kwh
    soc_kwh = (battery.soc_min + frac * (battery.soc_max - battery.soc_min)) * cap
    out = dispatch_hour(pv, load, soc_kwh, battery)
    eff = math.sqrt(battery.round_trip_efficiency)

    # AC-node conservation holds exactly (full precision, pre-quantisation).
    lhs = pv + out.grid_import + out.discharge
    rhs = load + out.grid_export + out.charge
    assert lhs == pytest.approx(rhs, abs=1e-6)

    # Non-negativity and mutual exclusivity of grid import/export.
    assert out.charge >= -1e-9 and out.discharge >= -1e-9
    assert out.grid_import >= -1e-9 and out.grid_export >= -1e-9
    assert min(out.grid_import, out.grid_export) == pytest.approx(0.0, abs=1e-9)

    # Power limits (energy over one hour == power in kW).
    assert out.charge <= battery.max_charge_kw + 1e-9
    assert out.discharge <= battery.max_discharge_kw + 1e-9

    # SoC continuity with split round-trip losses, and bounds never violated.
    expected_soc = soc_kwh + out.charge * eff - (out.discharge / eff if eff > 0 else 0.0)
    assert out.soc_kwh == pytest.approx(expected_soc, abs=1e-6)
    assert battery.soc_min * cap - 1e-6 <= out.soc_kwh <= battery.soc_max * cap + 1e-6


@settings(deadline=None, max_examples=75)
@given(
    ghi_values=st.lists(st.one_of(st.none(), st.floats(0.0, 1000.0)), min_size=24, max_size=24),
    temp_values=st.lists(st.one_of(st.none(), st.floats(-10.0, 40.0)), min_size=24, max_size=24),
    battery=_batteries,
    annual_load=st.floats(1000.0, 8000.0),
)
def test_simulated_day_conserves_energy_on_rounded_values(
    ghi_values: list[float | None],
    temp_values: list[float | None],
    battery: BatteryConfig,
    annual_load: float,
) -> None:
    instants = utc_hour_instants(WINDOW)
    ghi = dict(zip(instants, ghi_values, strict=True))
    temp = dict(zip(instants, temp_values, strict=True))
    synthetic = SyntheticConfig(annual_load_kwh=annual_load)

    series = simulate_day(DAY, WINDOW, ghi, temp, PV, battery, synthetic, THERMAL)
    by_ts: dict[Dataset, dict[int, float | None]] = {
        d: dict(points) for d, points in series.items()
    }

    for ts in instants:
        maybe = {d: by_ts[d][ts] for d in TELEMETRY_DATASETS}
        if any(v is None for v in maybe.values()):
            continue  # unsimulatable hour: surfaced as null, not asserted
        row = {d: v for d, v in maybe.items() if v is not None}  # all ten present here

        pv = row[Dataset.PV_PRODUCTION]
        load = row[Dataset.HOUSEHOLD_LOAD]
        base = row[Dataset.LOAD_BASE]
        ac = row[Dataset.AC_POWER]
        charge = row[Dataset.BATTERY_CHARGE]
        discharge = row[Dataset.BATTERY_DISCHARGE]
        imp = row[Dataset.GRID_IMPORT]
        exp = row[Dataset.GRID_EXPORT]
        soc = row[Dataset.SOC]
        assert pv >= 0 and load >= 0 and charge >= 0 and discharge >= 0
        assert base >= 0 and ac >= 0
        assert imp >= 0 and exp >= 0
        assert min(imp, exp) == 0.0  # mutually exclusive after rounding

        # The load split is tight to a *representation* limit, not to a modelling budget. The
        # generator quantises both components and publishes the quantised sum, so the only error
        # here is that a decimal 3-dp sum is not always a binary float sum -- 0.272 + 2.0 is
        # 2.2720000000000002, which is why this is approx and not `==`. One ULP, ~1e-16.
        #
        # The distinction matters: had the three values been rounded independently they could
        # disagree by a whole Wh (5e-4), seven orders of magnitude looser, and a tolerance sized
        # for that would hide real drift. This bound is the one the warehouse's
        # assert_load_split_identity uses, and it is 1e-9 there for the same reason.
        assert load == pytest.approx(base + ac, abs=1e-9)

        # AC-node balance on the rounded values. The battery dispatches against the published
        # total, so only pv/import/export/charge/discharge round independently of it -- and the
        # tolerance budgets those five 0.5 Wh quantisations, comfortably below any real imbalance.
        assert (pv + imp + discharge) == pytest.approx(load + exp + charge, abs=0.005)
        assert battery.soc_min - 1e-3 <= soc <= battery.soc_max + 1e-3


# -- The connector -----------------------------------------------------------------------


class _StubReader:
    """A ``WeatherReader`` serving fixed points, counting reads to prove memoisation."""

    def __init__(self, series: dict[str, tuple[Point, ...]]) -> None:
        self._series = series
        self.calls = 0

    def read_current_series(
        self,
        source: str,
        dataset: str,
        region: str,
        resolution: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[Point, ...]:
        self.calls += 1
        return tuple((ts, v) for ts, v in self._series.get(dataset, ()) if start_ms <= ts < end_ms)


def _reader_with_weather() -> _StubReader:
    return _StubReader(
        {
            Dataset.SHORTWAVE_RADIATION.value: tuple(_bell_ghi().items()),
            Dataset.TEMPERATURE_2M.value: tuple(_flat_temp().items()),
        }
    )


def test_client_fetches_each_dataset_and_matches_pure_simulation() -> None:
    reader = _reader_with_weather()
    client = SyntheticTelemetryClient(
        reader, pv=PV, battery=BATTERY, synthetic=SYNTHETIC, thermal=THERMAL
    )
    expected = simulate_day(DAY, WINDOW, _bell_ghi(), _flat_temp(), PV, BATTERY, SYNTHETIC, THERMAL)

    for dataset in TELEMETRY_DATASETS:
        raw = client.fetch_window(dataset, "home", Resolution.HOUR, WINDOW)
        assert raw.source == "synthetic"
        assert raw.points == expected[dataset]


def test_client_simulates_once_per_window() -> None:
    reader = _reader_with_weather()
    client = SyntheticTelemetryClient(
        reader, pv=PV, battery=BATTERY, synthetic=SYNTHETIC, thermal=THERMAL
    )
    for dataset in TELEMETRY_DATASETS:
        client.fetch_window(dataset, "home", Resolution.HOUR, WINDOW)
    # Seven datasets, one simulation: two reads (irradiance + temperature), not fourteen.
    assert reader.calls == 2


def test_client_rejects_non_hourly_resolution() -> None:
    client = SyntheticTelemetryClient(
        _reader_with_weather(), pv=PV, battery=BATTERY, synthetic=SYNTHETIC, thermal=THERMAL
    )
    with pytest.raises(SyntheticTelemetryError):
        client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.QUARTERHOUR, WINDOW)


def test_client_rejects_non_telemetry_dataset() -> None:
    client = SyntheticTelemetryClient(
        _reader_with_weather(), pv=PV, battery=BATTERY, synthetic=SYNTHETIC, thermal=THERMAL
    )
    with pytest.raises(SyntheticTelemetryError):
        client.fetch_window(Dataset.DAY_AHEAD_PRICE, "home", Resolution.HOUR, WINDOW)
