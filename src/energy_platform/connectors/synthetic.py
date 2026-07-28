"""Deterministic synthetic household telemetry -- the producer the public demo and CI run on.

The privacy invariant is that real telemetry never enters the repo, yet ``docker compose up``
must still show the full flow. This module squares that circle: it generates a day of hourly
PV / load / battery / grid telemetry as a **pure function of ``(config, partition_date)`` and
the already-ingested M2 weather**, so re-ingesting a day is a content-hash no-op exactly like a
real source, and demo mode is indistinguishable in *shape* from real mode.

Determinism is the whole point, so three rules are load-bearing and part of the data contract:

* **Seed recipe.** The per-day RNG seed is ``sha256(f"{salt}:{date}")`` truncated to 64 bits
  (:func:`seed_for`) -- never Python's per-process-salted ``hash()``. Changing the salt or the
  recipe changes every value and therefore every hash.
* **Single emission boundary.** All physics runs in full precision; :func:`emit` quantises each
  value to millimeter-. er, milliwatt-hour (3 dp in kWh) precision as the *last* step, so the
  quantisation policy is one documented, testable choice rather than scattered ``round`` calls.
* **stdlib only.** ``random.Random`` (a platform-stable Mersenne Twister) and ``math`` -- no
  numpy -- so float reprs stay identical across machines and the hash is bulletproof.

Energy is accounted at the AC coupling point in kWh, so three invariants hold every simulated
hour (they are what the property tests assert):

* **AC-node conservation (exact):**
  ``pv + grid_import + battery_discharge == load + grid_export + battery_charge``.
* **SoC continuity with round-trip losses:** stored energy changes by
  ``charge*sqrt(rte) - discharge/sqrt(rte)`` and never leaves ``[soc_min, soc_max]``.
* **The load split (exact, M10):** ``household_load == load_base + ac_power``. The air
  conditioner is a *component of* consumption, not an extra node term, so the AC-node identity
  above is untouched by the split. ``household_load`` stays the canonical total and is emitted
  independently rather than derived downstream -- a total computed as the sum of its parts could
  not be tested against them, and a real house with one consumption meter and no AC sub-meter
  reports the total with both components null.

M10 also adds a **conditioned zone**: a single-capacitance RC model driven by M2's outdoor
temperature and irradiance, with a bang-bang thermostat whose electrical draw is ``ac_power`` and
whose state is ``indoor_temperature``. It is deliberately the dumbest controller that is still a
thermostat, because it is the "actual behaviour" baseline M11's thermal optimiser will be measured
against -- the same role M3's naive battery plays for M6. Its parameters live in the shared
:class:`~energy_platform.config.ThermalConfig` for the same reason the battery's do: both sides of
a comparison must run on one physics.

Battery dispatch is *naive self-consumption* (charge PV surplus, discharge to cover deficit),
which becomes M6's "actual behaviour" baseline -- so it reads the same
:class:`~energy_platform.config.BatteryConfig` the optimiser will, keeping the savings
comparison like-for-like.

Missing inputs are surfaced, never fabricated: an hour whose GHI *or outdoor temperature* is
absent or null is emitted as ``None`` across *all* ten series, and the battery and the zone simply
idle across the gap so continuity resumes when inputs return. Outdoor temperature joined
irradiance in that predicate at M10: it used to be a second-order PV derate an hour could do
without, and it is now the RC model's driving input.

Per-partition purity has one deliberate modelling simplification, and M10 gives it a second
instance: each Berlin day is simulated independently from a fixed initial SoC *and* a fixed
initial zone temperature, because a day's telemetry must be reproducible from that one partition
date alone -- it cannot depend on the ending state of the day before, or re-ingesting one day in
isolation would no longer be a verifiable no-op.

The two resets need different answers, and assuming otherwise was a trap worth recording. A
battery reset to ``soc_min`` is survivable because a day of dispatch genuinely refills it. A zone
reset is not: the thermostat only acts *above* setpoint, so below it the zone free-runs with a
time constant of ``R*C`` -- longer than the day being simulated. Resetting to a constant would
therefore have stamped a one-day sawtooth onto every cool day, which is exactly the artefact this
series must not have if M11 is to learn from it. So the zone starts at the controller's
equilibrium for *that day's own* weather (:func:`initial_indoor_temp_c`) -- still a pure function
of the partition date, but a fixed point rather than a fixed value. How fast the remaining error
decays is asserted, not assumed: see ``test_initial_temperature_is_forgotten``.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from energy_platform.config import (
    BatteryConfig,
    PvSystemConfig,
    SyntheticConfig,
    ThermalConfig,
)
from energy_platform.connectors.types import Dataset, Point, RawSeries, Resolution, UtcWindow

SOURCE: Final = "synthetic"
# Values are computed at UTC hour boundaries (matching the weather instants they derive from);
# partitions still key off Berlin calendar days, as everywhere else.
SOURCE_TZ: Final = "UTC"

# The weather source the generator reads irradiance/temperature from (M2's Open-Meteo actuals).
WEATHER_SOURCE: Final = "open_meteo"

BERLIN: Final = ZoneInfo("Europe/Berlin")
_HOUR_MS: Final = 3_600_000

# The ten telemetry series, in a stable order. Like ``WEATHER_VARIABLES``, this is the set an
# asset or backfill loops over; each is ingested as its own single-valued series. It is also the
# single registry the real Home Assistant connector validates against, so a dataset added here and
# nowhere else is a dataset both producers agree exists.
TELEMETRY_DATASETS: Final[tuple[Dataset, ...]] = (
    Dataset.PV_PRODUCTION,
    Dataset.HOUSEHOLD_LOAD,
    Dataset.LOAD_BASE,
    Dataset.AC_POWER,
    Dataset.INDOOR_TEMPERATURE,
    Dataset.BATTERY_CHARGE,
    Dataset.BATTERY_DISCHARGE,
    Dataset.SOC,
    Dataset.GRID_IMPORT,
    Dataset.GRID_EXPORT,
)

# Quantisation granularity for the single emission boundary: 3 dp in kWh == 1 Wh, matching real
# meter resolution and keeping the content hash stable across platforms.
_QUANTUM_DP: Final = 3

# PV model constants. Cell temperature is estimated as air temperature plus a linear irradiance
# heating term (~NOCT 45 degC): ``(45-20)/800 ~= 0.031 degC per W/m^2``. Reference cell temp 25.
_CELL_TEMP_RISE_PER_WM2: Final = 0.031
_REFERENCE_CELL_TEMP_C: Final = 25.0

# Household load shape: relative hour-of-day weights (local Berlin hour), normalised per day.
# Weekdays have a sharp morning + evening peak; weekends are flatter and shifted later.
# fmt: off
_WEEKDAY_SHAPE: Final[tuple[float, ...]] = (
    0.50, 0.45, 0.40, 0.40, 0.45, 0.60,  # 00-05
    0.90, 1.30, 1.20, 1.00, 0.90, 0.90,  # 06-11
    0.95, 0.90, 0.85, 0.85, 0.95, 1.20,  # 12-17
    1.50, 1.60, 1.50, 1.20, 0.90, 0.65,  # 18-23
)
_WEEKEND_SHAPE: Final[tuple[float, ...]] = (
    0.60, 0.50, 0.45, 0.40, 0.40, 0.45,  # 00-05
    0.60, 0.80, 1.00, 1.15, 1.20, 1.20,  # 06-11
    1.15, 1.05, 1.00, 1.00, 1.05, 1.20,  # 12-17
    1.45, 1.55, 1.50, 1.30, 1.00, 0.75,  # 18-23
)
# Month (1..12) -> seasonal load multiplier (heating/lighting heavier in winter); averages ~1.0
# so the annual total stays near ``annual_load_kwh``.
_SEASONAL: Final[tuple[float, ...]] = (
    1.15, 1.12, 1.05, 0.98, 0.92, 0.85, 0.85, 0.88, 0.95, 1.03, 1.10, 1.18,
)
# fmt: on
_WEEKEND_LOAD_FACTOR: Final = 1.10  # households use a little more at the weekend
_LOAD_NOISE_AMPLITUDE: Final = 0.12  # +/-12% multiplicative, seeded per hour


class SyntheticTelemetryError(RuntimeError):
    """Raised when synthetic telemetry cannot be generated (e.g. an unsupported resolution)."""


@runtime_checkable
class WeatherReader(Protocol):
    """The read surface the generator needs: latest-ingestion series within a UTC window.

    :class:`~energy_platform.orchestration.raw_zone.RawZoneRepository` satisfies this, so the
    generator reads the *actually-ingested* M2 irradiance; tests pass a trivial stub.
    """

    def read_current_series(
        self,
        source: str,
        dataset: str,
        region: str,
        resolution: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[Point, ...]: ...


# -- Pure helpers (the data contract) --------------------------------------------------


def seed_for(salt: str, day: date) -> int:
    """Deterministic 64-bit RNG seed for a partition day. Part of the data contract."""
    digest = hashlib.sha256(f"{salt}:{day.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def quantise(value: float) -> float:
    """The emission boundary itself, for the values that are known to be present.

    Split out of :func:`emit` so the generator can quantise a component *before* summing it into a
    total (see :func:`simulate_day`) without threading ``None`` through arithmetic that cannot
    receive it.
    """
    rounded = round(value, _QUANTUM_DP)
    return rounded if rounded != 0 else 0.0


def emit(value: float | None) -> float | None:
    """Quantise one value at the single emission boundary; ``None`` passes through unchanged.

    Normalises a possible ``-0.0`` to ``0.0`` so the sign of zero never destabilises the hash.
    """
    return None if value is None else quantise(value)


def pv_production_kwh(ghi_wm2: float, temp_c: float | None, pv: PvSystemConfig) -> float:
    """Plausible hourly PV energy (kWh) from global horizontal irradiance.

    DC power scales linearly with irradiance, nameplate, and the performance ratio; a linear
    temperature derate around the 25 degC reference cell temperature is applied when air
    temperature is known (missing temperature -> no derate, a second-order effect not worth
    voiding an hour of PV over). Output is clipped at the inverter's AC ceiling and floored at 0.
    """
    dc_kw = pv.dc_kwp * (ghi_wm2 / 1000.0) * pv.performance_ratio
    if temp_c is None:
        derate = 1.0
    else:
        cell_temp = temp_c + _CELL_TEMP_RISE_PER_WM2 * ghi_wm2
        derate = 1.0 + pv.temp_coeff_per_c * (cell_temp - _REFERENCE_CELL_TEMP_C)
    ac_kw = min(dc_kw * derate, pv.ac_cap_kw)
    return max(ac_kw, 0.0)  # * 1 hour -> kWh


def household_load_kwh(ts_ms: int, rng: random.Random, synthetic: SyntheticConfig) -> float:
    """Deterministic household consumption (kWh) for one hour, plus one seeded noise draw.

    Shape depends on local Berlin hour, weekday/weekend, and month; the magnitude is scaled so a
    year sums to roughly ``annual_load_kwh``. Exactly one ``rng`` draw happens per call, so the
    RNG stream stays aligned to the hour grid regardless of missing weather.
    """
    local = datetime.fromtimestamp(ts_ms / 1000, BERLIN)
    is_weekend = local.weekday() >= 5
    shape = _WEEKEND_SHAPE if is_weekend else _WEEKDAY_SHAPE
    weight = shape[local.hour] / sum(shape)  # fraction of the day's load in this hour
    daytype = _WEEKEND_LOAD_FACTOR if is_weekend else 1.0
    daily_base = synthetic.annual_load_kwh / 365.25 * _SEASONAL[local.month - 1] * daytype
    noise = 1.0 + rng.uniform(-_LOAD_NOISE_AMPLITUDE, _LOAD_NOISE_AMPLITUDE)
    return daily_base * weight * noise


@dataclass(frozen=True, slots=True)
class _Thermal:
    """One hour of the conditioned zone: what the AC drew, where the zone ended, and its state.

    ``compressor_on`` is the thermostat's latch *after* this hour and is fed back into the next
    one -- a deadband without hysteresis is not a thermostat, it is a comparator that chatters.
    """

    ac_power_kwh: float
    indoor_temp_c: float
    compressor_on: bool


def thermostat_step(indoor_c: float, compressor_on: bool, thermal: ThermalConfig) -> bool:
    """Bang-bang cooling with hysteresis: the compressor's state for the coming hour.

    Turns on at ``setpoint + deadband/2`` and does not turn off again until ``setpoint -
    deadband/2``, so the latch state carries between hours. This is M10's "actual behaviour"
    baseline -- the thing M11's optimiser will be measured against -- and it is deliberately the
    dumbest controller that is still a thermostat: it cannot see prices, PV, or the next hour.
    """
    upper = thermal.setpoint_c + thermal.deadband_k / 2.0
    lower = thermal.setpoint_c - thermal.deadband_k / 2.0
    if compressor_on:
        return indoor_c > lower
    return indoor_c >= upper


def initial_indoor_temp_c(
    mean_outdoor_c: float, mean_ghi_wm2: float, thermal: ThermalConfig
) -> float:
    """Where the zone starts a Berlin day: the controller's equilibrium for that day's weather.

    A day's telemetry must be reproducible from its own partition date alone, so the zone cannot
    inherit yesterday's temperature (the same constraint that makes the battery start every day at
    ``soc_min``). But *which* fixed start is chosen matters far more here than it does for the
    battery, and the obvious answer is wrong.

    Starting every day at the cooling setpoint looks harmless and is not. The thermostat only acts
    when the zone is **above** setpoint; below it the zone free-runs with a time constant of
    ``R*C`` -- 48 h by default, longer than the day being simulated. So on every cool day the zone
    would spend all twenty-four hours decaying from an artificial 24 degC and never arrive,
    stamping a sawtooth with a one-day period onto a series whose whole purpose is to be a
    trustworthy oracle for M11. The battery's ``soc_min`` reset is survivable because a day of
    dispatch genuinely re-charges the battery; a thermal reset is not, because nothing re-heats
    the house back to the boundary condition.

    So the start is the steady state the *controller* holds for the day's mean conditions:
    free-running equilibrium ``T_out + solar_gain * GHI * R``, clamped into
    ``[heating_setpoint, cooling setpoint]`` -- the band the house is actually kept in. On a cold
    day that is the heating floor, on a hot day the cooling setpoint, and in between it is where a
    free-running house sits. Both inputs are means over the day's *own* ingested weather, so this
    is still a pure function of ``(config, partition_date, that day's weather)`` and re-ingesting
    one day in isolation is still a verifiable no-op.

    The residual error -- equilibrium-for-today versus the state genuinely carried from yesterday
    -- is what ``test_initial_temperature_is_forgotten`` bounds.
    """
    free_running = (
        mean_outdoor_c + thermal.solar_gain_kw_per_wm2 * mean_ghi_wm2 * thermal.r_k_per_kw
    )
    return min(max(free_running, thermal.heating_setpoint_c), thermal.setpoint_c)


def thermal_step(
    indoor_c: float,
    outdoor_c: float,
    ghi_wm2: float,
    compressor_on: bool,
    thermal: ThermalConfig,
) -> _Thermal:
    """Advance the single-zone RC model by one hour under thermostat control.

    Forward Euler on one capacitance, in kW and kWh so every term is an energy rate at the same
    scale as the rest of the generator::

        dT/dt = ( (T_out - T_in) / R  +  solar_gain * GHI  -  Q_cool ) / C

    ``Q_cool`` is ``rated_kw * cop`` while the compressor is latched on and zero otherwise, and
    the electrical draw reported as ``ac_power`` is ``rated_kw`` over the whole hour -- fully on
    or fully off. At hourly resolution that is a **coarse aggregate** of a unit that really cycles
    on a ten-minute period: the effective deadband is wider than the configured one by however far
    a full hour of cooling overshoots. Said plainly here rather than hidden behind a duty-cycle
    fudge, because M11 has to model this controller and needs to know what it actually is.

    The heating setpoint is applied last, as a floor on the resulting temperature. It is not a
    load: the house's heating is not electric, so it never appears in ``ac_power``.
    """
    on = thermostat_step(indoor_c, compressor_on, thermal)
    cooling_kw = thermal.rated_kw * thermal.cop if on else 0.0
    envelope_kw = (outdoor_c - indoor_c) / thermal.r_k_per_kw
    solar_kw = thermal.solar_gain_kw_per_wm2 * ghi_wm2
    # dt = 1 h, so a kW of net gain is a kWh, and dividing by C (kWh/K) gives K directly.
    next_c = indoor_c + (envelope_kw + solar_kw - cooling_kw) / thermal.c_kwh_per_k
    return _Thermal(
        ac_power_kwh=thermal.rated_kw if on else 0.0,
        indoor_temp_c=max(next_c, thermal.heating_setpoint_c),
        compressor_on=on,
    )


@dataclass(frozen=True, slots=True)
class _Dispatch:
    """One hour's naive self-consumption outcome, all in AC-side kWh (SoC in kWh)."""

    charge: float
    discharge: float
    grid_import: float
    grid_export: float
    soc_kwh: float


def dispatch_hour(pv: float, load: float, soc_kwh: float, battery: BatteryConfig) -> _Dispatch:
    """Naive self-consumption for one hour: charge PV surplus, discharge to cover any deficit.

    Round-trip efficiency is split symmetrically (each leg ``sqrt(rte)``): AC charge energy
    ``c`` stores ``c*sqrt(rte)``; delivering ``d`` of AC discharge removes ``d/sqrt(rte)`` from
    store. Charge is bounded by the power limit and the headroom to ``soc_max``; discharge by the
    power limit and the energy available above ``soc_min`` -- so SoC bounds can never be violated.
    By construction the AC node balances exactly and grid import/export are mutually exclusive.
    """
    eff = math.sqrt(battery.round_trip_efficiency)
    cap = battery.capacity_kwh
    soc_min_kwh = battery.soc_min * cap
    soc_max_kwh = battery.soc_max * cap
    max_charge = battery.max_charge_kw  # * 1 hour -> kWh
    max_discharge = battery.max_discharge_kw

    residual = pv - load
    if residual > 0:
        headroom_stored = soc_max_kwh - soc_kwh
        max_by_soc = headroom_stored / eff if eff > 0 else 0.0
        charge = min(residual, max_charge, max_by_soc)
        return _Dispatch(
            charge=charge,
            discharge=0.0,
            grid_import=0.0,
            grid_export=residual - charge,
            soc_kwh=soc_kwh + charge * eff,
        )
    if residual < 0:
        deficit = -residual
        available_stored = soc_kwh - soc_min_kwh
        max_by_soc = available_stored * eff
        discharge = min(deficit, max_discharge, max_by_soc)
        removed = discharge / eff if eff > 0 else 0.0
        return _Dispatch(
            charge=0.0,
            discharge=discharge,
            grid_import=deficit - discharge,
            grid_export=0.0,
            soc_kwh=soc_kwh - removed,
        )
    return _Dispatch(0.0, 0.0, 0.0, 0.0, soc_kwh)


def simulate_day(
    day: date,
    window: UtcWindow,
    ghi: dict[int, float | None],
    temperature: dict[int, float | None],
    pv: PvSystemConfig,
    battery: BatteryConfig,
    synthetic: SyntheticConfig,
    thermal: ThermalConfig,
) -> dict[Dataset, tuple[Point, ...]]:
    """Simulate one Berlin day into the ten telemetry series, sliced to ``window``.

    ``ghi`` / ``temperature`` map a UTC hour instant (epoch ms) to the ingested value (or
    ``None``). Every hour of the window gets a point per series -- with ``None`` across all ten
    where either input is missing -- so the series shape is complete and gaps are surfaced, not
    dropped.

    **Outdoor temperature became load-bearing at M10.** It used to be a second-order PV derate
    that an hour could do without; the RC model cannot run a step without it, and an hour with no
    zone temperature has no AC draw, hence no household load, hence no dispatch. So the
    non-simulatable predicate now covers both weather inputs rather than irradiance alone. In
    practice Open-Meteo delivers them together, so this costs no coverage; it is stated because a
    reader would otherwise assume the M3 rule still held.

    **The load split is exact by construction.** ``load_base`` and ``ac_power`` are quantised
    first and ``household_load`` is the quantised sum of the *already-quantised* components, so
    ``household_load == load_base + ac_power`` holds to floating-point dust rather than to within
    three independent roundings (which could disagree by a whole Wh -- 500,000x the tolerance the
    warehouse's sibling energy-conservation test uses). The battery then dispatches against that
    same published total, so the AC-node identity closes on the emitted numbers too.

    **The zone starts every day at the cooling setpoint**, for the reason the battery starts every
    day at ``soc_min``: a day's telemetry must be reproducible from its own partition date alone.
    Unlike the battery's, this reset is self-correcting -- a thermostat pulls the zone back into
    its deadband within about one time constant, and ``test_initial_temperature_is_forgotten``
    puts a number on how fast rather than leaving it as a caveat.
    """
    rng = random.Random(seed_for(synthetic.salt, day))
    series: dict[Dataset, list[Point]] = {d: [] for d in TELEMETRY_DATASETS}
    soc_kwh = battery.soc_min * battery.capacity_kwh  # fixed daily start (per-partition purity)
    hours = window.expected_count(Resolution.HOUR)

    # The zone's daily start, from this day's own weather -- see initial_indoor_temp_c for why a
    # constant would have stamped a one-day sawtooth onto the series.
    simulatable = [
        (ghi[ts], temperature[ts])
        for i in range(hours)
        if (ts := window.start_ms + i * _HOUR_MS) in ghi
        and ghi.get(ts) is not None
        and temperature.get(ts) is not None
    ]
    if simulatable:
        mean_ghi = sum(g for g, _ in simulatable if g is not None) / len(simulatable)
        mean_temp = sum(t for _, t in simulatable if t is not None) / len(simulatable)
        indoor_c = initial_indoor_temp_c(mean_temp, mean_ghi, thermal)
    else:
        indoor_c = thermal.setpoint_c  # no weather at all; every hour will be null anyway
    compressor_on = False

    for i in range(hours):
        ts = window.start_ms + i * _HOUR_MS
        base = household_load_kwh(ts, rng, synthetic)  # always drawn -> stream stays aligned
        ghi_value = ghi.get(ts)
        temp_value = temperature.get(ts)

        if ghi_value is None or temp_value is None:
            # Non-simulatable hour: surface a null across all series. The battery idles and the
            # zone holds (both carried, neither emitted) so continuity resumes once inputs return.
            for d in TELEMETRY_DATASETS:
                series[d].append((ts, None))
            continue

        zone = thermal_step(indoor_c, temp_value, ghi_value, compressor_on, thermal)
        indoor_c, compressor_on = zone.indoor_temp_c, zone.compressor_on

        base_kwh = quantise(base)
        ac_kwh = quantise(zone.ac_power_kwh)
        load_kwh = quantise(base_kwh + ac_kwh)  # exact: a 3-dp sum of two 3-dp values

        pv_kwh = pv_production_kwh(ghi_value, temp_value, pv)
        outcome = dispatch_hour(pv_kwh, load_kwh, soc_kwh, battery)
        soc_kwh = outcome.soc_kwh
        series[Dataset.PV_PRODUCTION].append((ts, emit(pv_kwh)))
        series[Dataset.HOUSEHOLD_LOAD].append((ts, load_kwh))
        series[Dataset.LOAD_BASE].append((ts, base_kwh))
        series[Dataset.AC_POWER].append((ts, ac_kwh))
        series[Dataset.INDOOR_TEMPERATURE].append((ts, emit(zone.indoor_temp_c)))
        series[Dataset.BATTERY_CHARGE].append((ts, emit(outcome.charge)))
        series[Dataset.BATTERY_DISCHARGE].append((ts, emit(outcome.discharge)))
        series[Dataset.SOC].append((ts, emit(soc_kwh / battery.capacity_kwh)))
        series[Dataset.GRID_IMPORT].append((ts, emit(outcome.grid_import)))
        series[Dataset.GRID_EXPORT].append((ts, emit(outcome.grid_export)))

    return {d: tuple(points) for d, points in series.items()}


# -- The connector ---------------------------------------------------------------------


class SyntheticTelemetryClient:
    """Generates telemetry as a :class:`MarketDataConnector`, so ``ingest_partition`` is unchanged.

    Like :class:`OpenMeteoArchiveClient`, it computes the whole window once (all ten coupled
    series) and memoises it, then returns the requested dataset's slice -- so a per-dataset
    ingestion loop runs the coupled simulation once per day, not ten times. It reads the
    already-ingested irradiance/temperature through the injected :class:`WeatherReader`.
    """

    source = SOURCE

    def __init__(
        self,
        reader: WeatherReader,
        *,
        pv: PvSystemConfig,
        battery: BatteryConfig,
        synthetic: SyntheticConfig,
        thermal: ThermalConfig,
        weather_source: str = WEATHER_SOURCE,
    ) -> None:
        self._reader = reader
        self._pv = pv
        self._battery = battery
        self._synthetic = synthetic
        self._thermal = thermal
        self._weather_source = weather_source
        # Memoise only the most recent window (all ten variables share it); a new window evicts
        # the previous one, keeping a long backfill flat in memory -- mirrors the archive client.
        self._cache: dict[tuple[str, int, int], dict[Dataset, tuple[Point, ...]]] = {}

    def fetch_window(
        self,
        dataset: Dataset,
        region: str,
        resolution: Resolution,
        window: UtcWindow,
    ) -> RawSeries:
        if resolution is not Resolution.HOUR:
            raise SyntheticTelemetryError(
                f"synthetic telemetry is hourly only; got resolution '{resolution.value}'"
            )
        if dataset not in TELEMETRY_DATASETS:
            raise SyntheticTelemetryError(f"'{dataset.value}' is not a telemetry dataset")

        series = self._simulate(region, window)
        return RawSeries(
            source=SOURCE,
            dataset=dataset,
            region=region,
            resolution=resolution,
            source_tz=SOURCE_TZ,
            source_urls=(self._descriptor(region, window),),
            points=series[dataset],
        )

    def _simulate(self, region: str, window: UtcWindow) -> dict[Dataset, tuple[Point, ...]]:
        key = (region, window.start_ms, window.end_ms)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        # The Berlin-local date of the window's start (a Berlin midnight) is the partition day.
        day = datetime.fromtimestamp(window.start_ms / 1000, BERLIN).date()
        ghi = self._read(Dataset.SHORTWAVE_RADIATION, region, window)
        temperature = self._read(Dataset.TEMPERATURE_2M, region, window)
        series = simulate_day(
            day, window, ghi, temperature, self._pv, self._battery, self._synthetic, self._thermal
        )
        self._cache.clear()
        self._cache[key] = series
        return series

    def _read(self, dataset: Dataset, region: str, window: UtcWindow) -> dict[int, float | None]:
        points = self._reader.read_current_series(
            self._weather_source,
            dataset.value,
            region,
            Resolution.HOUR.value,
            window.start_ms,
            window.end_ms,
        )
        return dict(points)

    def _descriptor(self, region: str, window: UtcWindow) -> str:
        """A stable non-network provenance string documenting the deterministic recipe."""
        day = datetime.fromtimestamp(window.start_ms / 1000, BERLIN).date()
        return f"synthetic://{region}/{day.isoformat()}?salt={self._synthetic.salt}"


def utc_hour_instants(window: UtcWindow) -> list[int]:
    """The UTC hour-boundary instants (epoch ms) spanning ``window`` -- shared by tests."""
    return [window.start_ms + i * _HOUR_MS for i in range(window.expected_count(Resolution.HOUR))]
