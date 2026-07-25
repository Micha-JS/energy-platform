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

Energy is accounted at the AC coupling point in kWh, so two invariants hold every simulated
hour (they are what the property tests assert):

* **AC-node conservation (exact):**
  ``pv + grid_import + battery_discharge == load + grid_export + battery_charge``.
* **SoC continuity with round-trip losses:** stored energy changes by
  ``charge*sqrt(rte) - discharge/sqrt(rte)`` and never leaves ``[soc_min, soc_max]``.

Battery dispatch is *naive self-consumption* (charge PV surplus, discharge to cover deficit),
which becomes M6's "actual behaviour" baseline -- so it reads the same
:class:`~energy_platform.config.BatteryConfig` the optimiser will, keeping the savings
comparison like-for-like.

Missing inputs are surfaced, never fabricated: an hour whose GHI is absent or null is emitted
as ``None`` across *all* seven series (the coupled sim cannot run without irradiance), and the
battery simply idles across the gap so continuity resumes when inputs return.

Per-partition purity has one deliberate modelling simplification: each Berlin day is simulated
independently from a fixed initial SoC, because a day's telemetry must be reproducible from that
one partition date alone -- it cannot depend on the ending SoC of the day before, or re-ingesting
one day in isolation would no longer be a verifiable no-op.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from energy_platform.config import BatteryConfig, PvSystemConfig, SyntheticConfig
from energy_platform.connectors.types import Dataset, Point, RawSeries, Resolution, UtcWindow

SOURCE: Final = "synthetic"
# Values are computed at UTC hour boundaries (matching the weather instants they derive from);
# partitions still key off Berlin calendar days, as everywhere else.
SOURCE_TZ: Final = "UTC"

# The weather source the generator reads irradiance/temperature from (M2's Open-Meteo actuals).
WEATHER_SOURCE: Final = "open_meteo"

BERLIN: Final = ZoneInfo("Europe/Berlin")
_HOUR_MS: Final = 3_600_000

# The seven telemetry series, in a stable order. Like ``WEATHER_VARIABLES``, this is the set an
# asset or backfill loops over; each is ingested as its own single-valued series.
TELEMETRY_DATASETS: Final[tuple[Dataset, ...]] = (
    Dataset.PV_PRODUCTION,
    Dataset.HOUSEHOLD_LOAD,
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


def emit(value: float | None) -> float | None:
    """Quantise one value at the single emission boundary; ``None`` passes through unchanged.

    Normalises a possible ``-0.0`` to ``0.0`` so the sign of zero never destabilises the hash.
    """
    if value is None:
        return None
    rounded = round(value, _QUANTUM_DP)
    return rounded if rounded != 0 else 0.0


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
) -> dict[Dataset, tuple[Point, ...]]:
    """Simulate one Berlin day into the seven telemetry series, sliced to ``window``.

    ``ghi`` / ``temperature`` map a UTC hour instant (epoch ms) to the ingested value (or
    ``None``). Every hour of the window gets a point per series -- with ``None`` across all seven
    where GHI is missing -- so the series shape is complete and gaps are surfaced, not dropped.
    """
    rng = random.Random(seed_for(synthetic.salt, day))
    series: dict[Dataset, list[Point]] = {d: [] for d in TELEMETRY_DATASETS}
    soc_kwh = battery.soc_min * battery.capacity_kwh  # fixed daily start (per-partition purity)
    hours = window.expected_count(Resolution.HOUR)

    for i in range(hours):
        ts = window.start_ms + i * _HOUR_MS
        load = household_load_kwh(ts, rng, synthetic)  # always drawn -> stream stays aligned
        ghi_value = ghi.get(ts)

        if ghi_value is None:
            # Non-simulatable hour: surface a null across all series; the battery idles (SoC
            # carried, not emitted) so continuity resumes once irradiance returns.
            for d in TELEMETRY_DATASETS:
                series[d].append((ts, None))
            continue

        pv_kwh = pv_production_kwh(ghi_value, temperature.get(ts), pv)
        outcome = dispatch_hour(pv_kwh, load, soc_kwh, battery)
        soc_kwh = outcome.soc_kwh
        series[Dataset.PV_PRODUCTION].append((ts, emit(pv_kwh)))
        series[Dataset.HOUSEHOLD_LOAD].append((ts, emit(load)))
        series[Dataset.BATTERY_CHARGE].append((ts, emit(outcome.charge)))
        series[Dataset.BATTERY_DISCHARGE].append((ts, emit(outcome.discharge)))
        series[Dataset.SOC].append((ts, emit(soc_kwh / battery.capacity_kwh)))
        series[Dataset.GRID_IMPORT].append((ts, emit(outcome.grid_import)))
        series[Dataset.GRID_EXPORT].append((ts, emit(outcome.grid_export)))

    return {d: tuple(points) for d, points in series.items()}


# -- The connector ---------------------------------------------------------------------


class SyntheticTelemetryClient:
    """Generates telemetry as a :class:`MarketDataConnector`, so ``ingest_partition`` is unchanged.

    Like :class:`OpenMeteoArchiveClient`, it computes the whole window once (all seven coupled
    series) and memoises it, then returns the requested dataset's slice -- so a per-dataset
    ingestion loop runs the coupled simulation once per day, not seven times. It reads the
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
        weather_source: str = WEATHER_SOURCE,
    ) -> None:
        self._reader = reader
        self._pv = pv
        self._battery = battery
        self._synthetic = synthetic
        self._weather_source = weather_source
        # Memoise only the most recent window (all seven variables share it); a new window evicts
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
            day, window, ghi, temperature, self._pv, self._battery, self._synthetic
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
