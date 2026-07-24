"""The shared, idempotent ingestion core.

Both the Dagster assets and the backfill CLI call :func:`ingest_partition`. Given a
market-data connector and a raw-zone repository, it fetches one Europe/Berlin calendar
day, content-hashes the result, and persists it append-only -- so re-running via either
entry point is a verifiable no-op when the source data is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from energy_platform.connectors.base import MarketDataConnector
from energy_platform.connectors.types import Dataset, Point, Resolution, UtcWindow
from energy_platform.orchestration.raw_zone import (
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
