"""Integration tests for the append-only forecast raw zone against a real Postgres.

Marked ``postgres``; skipped automatically when no database is reachable (see the
``postgres_conn`` fixture). These prove the vintage discipline: distinct issue dates
accumulate as independent truths and are never collapsed to "latest".
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest

from energy_platform.connectors.open_meteo import OpenMeteoForecastClient
from energy_platform.connectors.types import Point, Resolution
from energy_platform.orchestration.ingest import ingest_forecast_vintage
from energy_platform.orchestration.raw_zone import (
    ForecastIngestionRecord,
    RawZoneRepository,
    WriteOutcome,
)

pytestmark = pytest.mark.postgres

SCHEMA = "raw_test"
ISSUE = date(2026, 7, 24)
T1 = datetime(2026, 7, 24, 6, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)  # a later fetch, same issue day

Conn = psycopg.Connection[tuple[object, ...]]


def _scalar(conn: Conn, query: str, params: tuple[object, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _rows(conn: Conn, query: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _ledger_count(conn: Conn) -> int:
    return int(_scalar(conn, f"SELECT count(*) FROM {SCHEMA}.forecast_ingestion"))


def _record(
    content_hash: str,
    payload: dict[str, list[Point]],
    *,
    issue_date: date = ISSUE,
    issue_time: datetime = T1,
    region: str = "home",
) -> ForecastIngestionRecord:
    row_count = sum(len(points) for points in payload.values())
    null_count = sum(1 for points in payload.values() for _, v in points if v is None)
    return ForecastIngestionRecord(
        source="open_meteo",
        region=region,
        resolution=Resolution.HOUR.value,
        issue_date=issue_date,
        issue_time=issue_time,
        horizon_days=7,
        source_urls=["http://fixture"],
        payload=payload,
        content_hash=content_hash,
        row_count=row_count,
        null_count=null_count,
    )


# -- Vintage discipline at the repository layer ----------------------------------


def test_identical_content_same_issue_date_is_noop_regardless_of_issue_time(
    raw_repo: RawZoneRepository, postgres_conn: Conn
) -> None:
    """issue_time is excluded from the key, so re-seeing the same forecast is a verifiable no-op."""
    first = _record("hash_v1", {"temperature_2m": [(1_000, 20.0)]}, issue_time=T1)
    again = _record("hash_v1", {"temperature_2m": [(1_000, 20.0)]}, issue_time=T2)

    assert raw_repo.write_forecast_ingestion(first) is WriteOutcome.LOADED
    assert raw_repo.write_forecast_ingestion(again) is WriteOutcome.NOOP
    assert _ledger_count(postgres_conn) == 1  # no spurious vintage from a later identical fetch


def test_changed_content_same_issue_date_appends_a_revision(
    raw_repo: RawZoneRepository, postgres_conn: Conn
) -> None:
    first = _record("hash_v1", {"temperature_2m": [(1_000, 20.0)]}, issue_time=T1)
    revised = _record("hash_v2", {"temperature_2m": [(1_000, 20.5)]}, issue_time=T2)

    assert raw_repo.write_forecast_ingestion(first) is WriteOutcome.LOADED
    assert raw_repo.write_forecast_ingestion(revised) is WriteOutcome.REVISION
    assert _ledger_count(postgres_conn) == 2  # both versions retained (append-only)
    assert (
        raw_repo.latest_forecast_hash("open_meteo", "home", Resolution.HOUR.value, ISSUE)
        == "hash_v2"
    )


def test_distinct_issue_dates_accumulate_as_independent_vintages(
    raw_repo: RawZoneRepository, postgres_conn: Conn
) -> None:
    """Two issue dates predicting the SAME target instant both persist -- no latest-wins collapse.

    This is the no-lookahead guarantee made physical: a backtest can ask "what did the vintage
    issued on D predict for T?" and get D's answer, not the newest one.
    """
    target_ms = 1_000
    v1 = _record("h1", {"temperature_2m": [(target_ms, 20.0)]}, issue_date=date(2026, 7, 24))
    v2 = _record("h2", {"temperature_2m": [(target_ms, 21.0)]}, issue_date=date(2026, 7, 25))

    assert raw_repo.write_forecast_ingestion(v1) is WriteOutcome.LOADED
    # A different issue date is a new vintage, never a REVISION of the earlier one.
    assert raw_repo.write_forecast_ingestion(v2) is WriteOutcome.LOADED
    assert _ledger_count(postgres_conn) == 2

    target_ts = datetime.fromtimestamp(1, tz=UTC)  # 1_000 ms -> 1 s
    rows = _rows(
        postgres_conn,
        f"""
        SELECT issue_date, value FROM {SCHEMA}.forecast_observations
        WHERE target_ts_utc = %s ORDER BY issue_date
        """,
        (target_ts,),
    )
    assert rows == [(date(2026, 7, 24), 20.0), (date(2026, 7, 25), 21.0)]


def test_null_forecast_values_are_stored_as_missing(
    raw_repo: RawZoneRepository, postgres_conn: Conn
) -> None:
    record = _record("hash_null", {"cloud_cover": [(1_000, None), (4_600, 50.0)]})
    assert raw_repo.write_forecast_ingestion(record) is WriteOutcome.LOADED

    missing = int(
        _scalar(
            postgres_conn, f"SELECT count(*) FROM {SCHEMA}.forecast_observations WHERE is_missing"
        )
    )
    assert missing == 1


# -- End to end through the ingest core + fixture client -------------------------


def test_ingest_forecast_vintage_end_to_end(
    raw_repo: RawZoneRepository,
    postgres_conn: Conn,
    open_meteo_forecast_client: OpenMeteoForecastClient,
) -> None:
    result = ingest_forecast_vintage(
        open_meteo_forecast_client, raw_repo, "home", Resolution.HOUR, ISSUE, T1
    )
    assert result.outcome is WriteOutcome.LOADED
    assert result.variable_count == 6
    assert result.row_count == 6 * 192  # six variables over the 8-day horizon
    assert result.null_count == 1

    obs_count = int(_scalar(postgres_conn, f"SELECT count(*) FROM {SCHEMA}.forecast_observations"))
    assert obs_count == result.row_count

    # Re-running the same issue date -- even at a later issue_time -- is a hash-verified no-op.
    again = ingest_forecast_vintage(
        open_meteo_forecast_client, raw_repo, "home", Resolution.HOUR, ISSUE, T2
    )
    assert again.outcome is WriteOutcome.NOOP
    assert again.content_hash == result.content_hash
    assert _ledger_count(postgres_conn) == 1
