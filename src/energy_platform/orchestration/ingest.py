"""The shared, idempotent ingestion core.

Both the Dagster assets and the backfill CLI call :func:`ingest_partition`. Given a
market-data connector and a raw-zone repository, it fetches one Europe/Berlin calendar
day, content-hashes the result, and persists it append-only -- so re-running via either
entry point is a verifiable no-op when the source data is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from energy_platform.connectors.base import MarketDataConnector
from energy_platform.connectors.open_meteo import WEATHER_VARIABLES, OpenMeteoForecastClient
from energy_platform.connectors.synthetic import WEATHER_SOURCE
from energy_platform.connectors.types import Dataset, Point, Resolution, UtcWindow
from energy_platform.orchestration.raw_zone import (
    ForecastIngestionRecord,
    IngestionRecord,
    RawZoneRepository,
    WriteOutcome,
)

BERLIN = ZoneInfo("Europe/Berlin")


def berlin_day_window(day: date) -> UtcWindow:
    """UTC instant window for one Europe/Berlin calendar day.

    Uses calendar-date arithmetic then converts to UTC, so DST-transition days correctly
    span 23 hours (last Sunday of March) or 25 hours (last Sunday of October) rather than
    a naive fixed 24. Midnight is never inside a DST gap, so both bounds are unambiguous.
    """
    start_local = datetime.combine(day, time.min, tzinfo=BERLIN)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=BERLIN)
    # ``datetime.timestamp()`` already yields correct POSIX time for any aware datetime.
    start_ms = int(start_local.timestamp() * 1000)
    end_ms = int(end_local.timestamp() * 1000)
    return UtcWindow(start_ms=start_ms, end_ms=end_ms)


def content_hash(
    dataset: Dataset,
    region: str,
    resolution: Resolution,
    partition_date: date,
    points: tuple[Point, ...],
) -> str:
    """Deterministic sha256 over the partition's identity and its series slice.

    Python's ``json`` uses the shortest round-tripping float repr, so identical values
    serialise identically across runs and platforms -- the hash is a stable idempotency
    signal, and a single changed value produces a different digest.
    """
    canonical = json.dumps(
        {
            "dataset": dataset.value,
            "region": region,
            "resolution": resolution.value,
            "partition_date": partition_date.isoformat(),
            "points": [[ts, value] for ts, value in points],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of ingesting a single partition, for asset metadata and CLI summaries."""

    dataset: Dataset
    partition_date: date
    outcome: WriteOutcome
    content_hash: str
    row_count: int
    null_count: int
    expected_count: int
    hours_in_day: int

    @property
    def is_noop(self) -> bool:
        return self.outcome is WriteOutcome.NOOP

    @property
    def has_missing(self) -> bool:
        """True if any interval is null or absent -- surfaced, never fabricated."""
        return self.null_count > 0 or self.row_count < self.expected_count


def ingest_partition(
    client: MarketDataConnector,
    repo: RawZoneRepository,
    dataset: Dataset,
    region: str,
    resolution: Resolution,
    day: date,
    *,
    dagster_run_id: str | None = None,
) -> IngestResult:
    window = berlin_day_window(day)
    series = client.fetch_window(dataset, region, resolution, window)
    digest = content_hash(dataset, region, resolution, day, series.points)

    record = IngestionRecord(
        source=series.source,
        dataset=dataset.value,
        region=region,
        resolution=resolution.value,
        partition_date=day,
        source_tz=series.source_tz,
        source_urls=list(series.source_urls),
        payload=list(series.points),
        content_hash=digest,
        row_count=series.row_count,
        null_count=series.null_count,
        expected_count=window.expected_count(resolution),
        hours_in_day=window.hours,
        dagster_run_id=dagster_run_id,
    )
    outcome = repo.write_ingestion(record)

    return IngestResult(
        dataset=dataset,
        partition_date=day,
        outcome=outcome,
        content_hash=digest,
        row_count=record.row_count,
        null_count=record.null_count,
        expected_count=record.expected_count,
        hours_in_day=record.hours_in_day,
    )


class WeatherDependencyError(RuntimeError):
    """Raised when synthetic telemetry is requested for days whose weather is not yet ingested.

    Synthetic PV derives from that day's ingested irradiance; a missing day would silently yield
    all-null PV. Both the CLI backfill and the Dagster telemetry asset call
    :func:`require_weather_ingested` so they fail identically -- loudly, never green-with-nulls.
    """


def require_weather_ingested(
    repo: RawZoneRepository,
    site_id: str,
    days: Sequence[date],
    *,
    resolution: Resolution = Resolution.HOUR,
    hint: str | None = None,
) -> None:
    """Raise :class:`WeatherDependencyError` if any of ``days`` lacks ingested weather.

    Probes the shortwave-radiation series (the irradiance PV derives from); presence of its latest
    hash is the signal that the day's weather actuals are in the raw zone. ``hint`` appends
    entry-point-specific remediation advice to the message.
    """
    probe = Dataset.SHORTWAVE_RADIATION.value
    missing = [
        day
        for day in days
        if repo.latest_hash(WEATHER_SOURCE, probe, site_id, resolution.value, day) is None
    ]
    if not missing:
        return
    message = (
        f"telemetry requires ingested weather for site '{site_id}', but "
        f"{len(missing)} of {len(days)} day(s) have none (first: {missing[0].isoformat()})."
    )
    if hint:
        message = f"{message} {hint}"
    raise WeatherDependencyError(message)


# -- Forecast vintages -----------------------------------------------------------------


def forecast_content_hash(
    source: str,
    region: str,
    resolution: Resolution,
    issue_date: date,
    series: Mapping[Dataset, tuple[Point, ...]],
) -> str:
    """Deterministic sha256 over a forecast vintage's identity and its values.

    Variables are sorted so ordering never affects the digest, and ``issue_time`` is
    deliberately excluded: an identical forecast re-fetched moments later hashes the same, so
    it is a verifiable no-op rather than a spurious new vintage. A single changed value, or a
    ``None`` where a number was, changes the digest.
    """
    canonical = json.dumps(
        {
            "source": source,
            "region": region,
            "resolution": resolution.value,
            "issue_date": issue_date.isoformat(),
            "series": {
                variable.value: [[ts, value] for ts, value in series[variable]]
                for variable in sorted(series, key=lambda d: d.value)
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ForecastIngestResult:
    """Outcome of ingesting a single forecast vintage, for asset metadata / CLI summaries."""

    issue_date: date
    outcome: WriteOutcome
    content_hash: str
    row_count: int
    null_count: int
    variable_count: int
    horizon_days: int

    @property
    def is_noop(self) -> bool:
        return self.outcome is WriteOutcome.NOOP

    @property
    def has_missing(self) -> bool:
        """True if any forecast value is null -- surfaced, never fabricated."""
        return self.null_count > 0


def ingest_forecast_vintage(
    client: OpenMeteoForecastClient,
    repo: RawZoneRepository,
    site_id: str,
    resolution: Resolution,
    issue_day: date,
    issue_time: datetime,
    *,
    variables: Sequence[Dataset] = WEATHER_VARIABLES,
    dagster_run_id: str | None = None,
) -> ForecastIngestResult:
    """Fetch the current forecast horizon and persist it as one immutable vintage.

    ``issue_time`` is the caller-supplied as-of instant (``datetime.now(UTC)`` in production, a
    fixed instant in tests), stored so backtests can reconstruct exactly what was known when.
    """
    forecast = client.fetch_forecast(site_id, resolution, variables)
    digest = forecast_content_hash(forecast.source, site_id, resolution, issue_day, forecast.series)
    payload = {variable.value: list(points) for variable, points in forecast.series.items()}

    record = ForecastIngestionRecord(
        source=forecast.source,
        region=site_id,
        resolution=resolution.value,
        issue_date=issue_day,
        issue_time=issue_time,
        horizon_days=client.forecast_days,
        source_urls=list(forecast.source_urls),
        payload=payload,
        content_hash=digest,
        row_count=forecast.row_count,
        null_count=forecast.null_count,
        dagster_run_id=dagster_run_id,
    )
    outcome = repo.write_forecast_ingestion(record)

    return ForecastIngestResult(
        issue_date=issue_day,
        outcome=outcome,
        content_hash=digest,
        row_count=record.row_count,
        null_count=record.null_count,
        variable_count=len(forecast.series),
        horizon_days=record.horizon_days,
    )
