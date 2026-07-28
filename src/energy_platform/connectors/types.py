"""Shared types for market-data connectors.

These are source-agnostic: SMARD implements them today, ENTSO-E can implement the same
:class:`~energy_platform.connectors.base.MarketDataConnector` protocol later without any
change to the ingestion core or raw zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# A single observation as received from the source: (epoch milliseconds UTC, value|null).
# ``None`` means the source explicitly reported no value for that instant -- it is
# preserved verbatim and never fabricated or interpolated.
Point = tuple[int, float | None]


class Dataset(StrEnum):
    """A time-series we ingest.

    Market-data series (SMARD) and weather variables (Open-Meteo) share this enum: the
    ingestion core and raw zone are source-agnostic and treat ``dataset`` as an opaque
    string, so each weather variable is simply its own single-valued series -- no change to
    :class:`Point`, the content hash, or the ``observations`` table.
    """

    # -- Market data (SMARD) --
    DAY_AHEAD_PRICE = "day_ahead_price"
    GRID_LOAD = "grid_load"

    # -- Weather (Open-Meteo), hourly, values in UTC --
    SHORTWAVE_RADIATION = "shortwave_radiation"  # GHI, W/m^2
    DIRECT_RADIATION = "direct_radiation"  # W/m^2
    DIFFUSE_RADIATION = "diffuse_radiation"  # W/m^2
    TEMPERATURE_2M = "temperature_2m"  # deg C
    CLOUD_COVER = "cloud_cover"  # %
    WIND_SPEED_10M = "wind_speed_10m"  # m/s

    # -- Household telemetry (synthetic demo or real Fenecon), hourly, energy in kWh --
    # Accounted at the AC coupling point so the node identity holds every hour:
    #   pv_production + grid_import + battery_discharge
    #     == household_load + grid_export + battery_charge
    # (round-trip losses are internal to the battery -- see the SoC continuity invariant).
    PV_PRODUCTION = "pv_production"  # PV energy delivered to the AC bus, kWh
    HOUSEHOLD_LOAD = "household_load"  # consumption, kWh
    BATTERY_CHARGE = "battery_charge"  # AC energy drawn to charge the battery, kWh
    BATTERY_DISCHARGE = "battery_discharge"  # AC energy delivered from the battery, kWh
    SOC = "soc"  # state of charge at end of hour, fraction in [soc_min, soc_max]
    GRID_IMPORT = "grid_import"  # energy imported at the meter, kWh
    GRID_EXPORT = "grid_export"  # energy exported at the meter, kWh

    # -- Thermal channels (M10) --
    # HOUSEHOLD_LOAD is separable into everything-else and the air conditioner, so that M11 can
    # treat cooling as a flexible load without having to guess how much of the total it already
    # was. The split is a second identity, checked wherever all three are present:
    #   household_load == load_base + ac_power
    # HOUSEHOLD_LOAD remains the canonical total and keeps its place in the AC-node identity
    # above: `ac_power` is a *component of* consumption, not an additional node term. Both
    # components are null where a producer cannot separate them -- a real house with one
    # consumption meter and no AC sub-meter reports the total and nothing else.
    LOAD_BASE = "load_base"  # consumption excluding the air conditioner, kWh
    AC_POWER = "ac_power"  # electrical energy drawn by the air conditioner, kWh
    INDOOR_TEMPERATURE = "indoor_temperature"  # zone air temperature at end of hour, deg C


class Resolution(StrEnum):
    """Temporal resolution of a series, matching SMARD's URL tokens."""

    HOUR = "hour"
    QUARTERHOUR = "quarterhour"

    @property
    def steps_per_hour(self) -> int:
        return 1 if self is Resolution.HOUR else 4

    @property
    def step_seconds(self) -> int:
        return 3600 // self.steps_per_hour


@dataclass(frozen=True, slots=True)
class UtcWindow:
    """A half-open UTC instant window ``[start_ms, end_ms)`` in epoch milliseconds.

    Built from a calendar day so that DST-transition days span 23 or 25 hours; see
    :func:`energy_platform.orchestration.ingest.berlin_day_window`.
    """

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.end_ms <= self.start_ms:
            raise ValueError(f"empty window: [{self.start_ms}, {self.end_ms})")

    @property
    def duration_seconds(self) -> int:
        return (self.end_ms - self.start_ms) // 1000

    @property
    def hours(self) -> int:
        return self.duration_seconds // 3600

    def expected_count(self, resolution: Resolution) -> int:
        """Number of intervals a complete series would contain for this window."""
        return self.duration_seconds // resolution.step_seconds

    def contains(self, ts_ms: int) -> bool:
        return self.start_ms <= ts_ms < self.end_ms


@dataclass(frozen=True, slots=True)
class RawSeries:
    """A source series sliced to a requested window, as received (never mutated).

    ``points`` are ordered by timestamp and contain exactly what the source returned for
    the window -- including ``None`` values. Missing intervals stay missing; their absence
    is surfaced downstream by comparing ``len(points)`` against the window's expected count.
    """

    source: str
    dataset: Dataset
    region: str
    resolution: Resolution
    source_tz: str
    source_urls: tuple[str, ...]
    points: tuple[Point, ...]

    @property
    def row_count(self) -> int:
        return len(self.points)

    @property
    def null_count(self) -> int:
        return sum(1 for _, value in self.points if value is None)


@dataclass(frozen=True, slots=True)
class RawForecast:
    """A single forecast vintage: every variable's full horizon, as received in one snapshot.

    Unlike :class:`RawSeries` -- one windowed variable of settled truth -- a forecast is pulled
    as a multi-variable horizon and stored as one immutable vintage keyed by its issue instant.
    ``series`` maps each variable to its points; timestamps are UTC instants spanning the
    horizon, and ``None`` values are preserved verbatim, never fabricated or interpolated.
    """

    source: str
    region: str  # site id
    resolution: Resolution
    source_tz: str
    source_urls: tuple[str, ...]
    series: dict[Dataset, tuple[Point, ...]]

    @property
    def row_count(self) -> int:
        return sum(len(points) for points in self.series.values())

    @property
    def null_count(self) -> int:
        return sum(1 for points in self.series.values() for _, value in points if value is None)
