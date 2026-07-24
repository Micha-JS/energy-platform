"""Integration tests for the append-only raw zone against a real Postgres.

Marked ``postgres``; skipped automatically when no database is reachable (see the
``postgres_conn`` fixture). CI runs these against a service container to prove idempotency.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest

from energy_platform.connectors.smard import SmardClient
from energy_platform.connectors.types import Dataset, Point, Resolution
from energy_platform.orchestration.ingest import ingest_partition
from energy_platform.orchestration.raw_zone import (
    IngestionRecord,
    RawZoneRepository,
    WriteOutcome,
)

pytestmark = pytest.mark.postgres

SCHEMA = "raw_test"
DAY = date(2024, 6, 12)

Conn = psycopg.Connection[tuple[object, ...]]


def _scalar(conn: Conn, query: str, params: tuple[object, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _ledger_count(conn: Conn) -> int:
    return int(_scalar(conn, f"SELECT count(*) FROM {SCHEMA}.ingestion"))


# -- ingest_partition end to end (fixtures + real DB) ----------------------------


def test_first_ingest_loads_and_populates_observations(
    raw_repo: RawZoneRepository,
    postgres_conn: Conn,
    smard_client: SmardClient,
) -> None:
    result = ingest_partition(
        smard_client, raw_repo, Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, DAY
    )

    assert result.outcome is WriteOutcome.LOADED
    assert not result.is_noop
    assert result.row_count == 24
    assert _ledger_count(postgres_conn) == 1
    obs_count = int(_scalar(postgres_conn, f"SELECT count(*) FROM {SCHEMA}.observations"))
    assert obs_count == result.row_count
    missing = int(
        _scalar(postgres_conn, f"SELECT count(*) FROM {SCHEMA}.observations WHERE is_missing")
    )
    assert missing == result.null_count


def test_reingesting_identical_content_is_a_verifiable_noop(
    raw_repo: RawZoneRepository,
    postgres_conn: Conn,
    smard_client: SmardClient,
) -> None:
    first = ingest_partition(smard_client, raw_repo, Dataset.GRID_LOAD, "DE", Resolution.HOUR, DAY)
    second = ingest_partition(smard_client, raw_repo, Dataset.GRID_LOAD, "DE", Resolution.HOUR, DAY)

    assert first.outcome is WriteOutcome.LOADED
    assert second.outcome is WriteOutcome.NOOP
    assert second.is_noop
    assert first.content_hash == second.content_hash  # hashes prove the no-op
    assert _ledger_count(postgres_conn) == 1  # nothing appended on the second run


# -- Revision handling at the repository layer -----------------------------------


def _record(content_hash: str, payload: list[Point], *, source: str = "smard") -> IngestionRecord:
    return IngestionRecord(
        source=source,
        dataset=Dataset.DAY_AHEAD_PRICE.value,
        region="DE",
        resolution=Resolution.HOUR.value,
        partition_date=DAY,
        source_tz="Europe/Berlin",
        source_urls=["http://fixture"],
        payload=payload,
        content_hash=content_hash,
        row_count=len(payload),
        null_count=sum(1 for _, v in payload if v is None),
        expected_count=len(payload),
        hours_in_day=24,
    )


def test_revision_appends_a_new_version_without_mutating_history(
    raw_repo: RawZoneRepository, postgres_conn: Conn
) -> None:
    first = _record("hash_v1", [(1_000, 10.0)])
    revised = _record("hash_v2", [(1_000, 11.0)])

    assert raw_repo.write_ingestion(first) is WriteOutcome.LOADED
    assert raw_repo.write_ingestion(first) is WriteOutcome.NOOP
    assert raw_repo.write_ingestion(revised) is WriteOutcome.REVISION

    assert _ledger_count(postgres_conn) == 2  # both versions retained (append-only)
    latest_hash = raw_repo.latest_hash(
        "smard", Dataset.DAY_AHEAD_PRICE.value, "DE", Resolution.HOUR.value, DAY
    )
    assert latest_hash == "hash_v2"


def test_different_sources_on_same_partition_do_not_collide(
    raw_repo: RawZoneRepository, postgres_conn: Conn
) -> None:
    """`source` is part of the idempotency key, so a second connector on the same
    (dataset, region, resolution, date) is a fresh LOAD -- never a false NOOP or REVISION."""
    smard = _record("hash_smard", [(1_000, 10.0)], source="smard")
    entsoe = _record("hash_entsoe", [(1_000, 11.0)], source="entsoe")

    assert raw_repo.write_ingestion(smard) is WriteOutcome.LOADED
    assert raw_repo.write_ingestion(entsoe) is WriteOutcome.LOADED  # not a REVISION of smard

    assert _ledger_count(postgres_conn) == 2

    def latest(source: str) -> str | None:
        return raw_repo.latest_hash(
            source, Dataset.DAY_AHEAD_PRICE.value, "DE", Resolution.HOUR.value, DAY
        )

    assert latest("smard") == "hash_smard"
    assert latest("entsoe") == "hash_entsoe"


def test_observations_current_resolves_to_the_latest_version(
    raw_repo: RawZoneRepository, postgres_conn: Conn
) -> None:
    raw_repo.write_ingestion(_record("hash_v1", [(1_000, 10.0)]))
    raw_repo.write_ingestion(_record("hash_v2", [(1_000, 11.0)]))

    ts_utc = datetime.fromtimestamp(1, tz=UTC)  # 1_000 ms -> 1 s
    current_value = _scalar(
        postgres_conn,
        f"SELECT value FROM {SCHEMA}.observations_current WHERE ts_utc = %s",
        (ts_utc,),
    )
    assert current_value == 11.0
    total_current = int(
        _scalar(postgres_conn, f"SELECT count(*) FROM {SCHEMA}.observations_current")
    )
    assert total_current == 1  # the superseded version is hidden by the view
