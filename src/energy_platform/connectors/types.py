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
    """A market-data series we ingest."""

    DAY_AHEAD_PRICE = "day_ahead_price"
    GRID_LOAD = "grid_load"


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
