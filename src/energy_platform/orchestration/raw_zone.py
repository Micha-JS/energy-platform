"""Append-only Postgres raw zone for market-data ingestions.

Two tables in a dedicated schema (default ``raw``):

* ``ingestion`` -- one append-only row per successful partition ingestion, storing the
  day's series slice as received (JSONB), a sha256 content hash, and observability counts.
  A unique index on
  ``(source, dataset, region, resolution, partition_date, content_hash)`` is the
  idempotency key: re-ingesting identical content is a verifiable no-op. ``source`` is part
  of the key so a second connector (e.g. ENTSO-E) never collides with SMARD's rows.
* ``observations`` -- a faithful long-form unnest of each ingestion's payload
  (``ts_utc``, ``value``, ``is_missing``). The source epoch-ms is already a UTC instant, so
  this is a mechanical mirror, not a transformation.

The ``observations_current`` view exposes the latest ingestion per partition so a SMARD
revision (a new content hash for a day already loaded) appends a new version without
mutating history, and readers still see one clean series.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from energy_platform.connectors.types import Point

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class WriteOutcome(StrEnum):
    """Result of attempting to persist an ingestion."""

    LOADED = "loaded"  # first time this partition was ingested
    NOOP = "noop"  # identical content already present -- nothing written
    REVISION = "revision"  # partition existed with different content -- new version appended


@dataclass(frozen=True, slots=True)
class IngestionRecord:
    """Everything needed to persist one partition ingestion."""

    source: str
    dataset: str
    region: str
    resolution: str
    partition_date: date
    source_tz: str
    source_urls: list[str]
    payload: list[Point]
    content_hash: str
    row_count: int
    null_count: int
    expected_count: int
    hours_in_day: int
    dagster_run_id: str | None = None


def _ddl(schema: str) -> list[sql.Composed]:
    ident = sql.Identifier(schema)
    return [
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=ident),
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {schema}.ingestion (
                id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                source          text        NOT NULL,
                dataset         text        NOT NULL,
                region          text        NOT NULL,
                resolution      text        NOT NULL,
                partition_date  date        NOT NULL,
                source_tz       text        NOT NULL,
                source_urls     jsonb       NOT NULL,
                payload         jsonb       NOT NULL,
                content_hash    text        NOT NULL,
                row_count       integer     NOT NULL,
                null_count      integer     NOT NULL,
                expected_count  integer     NOT NULL,
                hours_in_day    integer     NOT NULL,
                fetched_at      timestamptz NOT NULL DEFAULT now(),
                dagster_run_id  text
            )
            """
        ).format(schema=ident),
        sql.SQL(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ingestion_content_key
            ON {schema}.ingestion
                (source, dataset, region, resolution, partition_date, content_hash)
            """
        ).format(schema=ident),
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {schema}.observations (
                ingestion_id bigint NOT NULL
                    REFERENCES {schema}.ingestion (id) ON DELETE CASCADE,
                dataset      text        NOT NULL,
                region       text        NOT NULL,
                resolution   text        NOT NULL,
                ts_utc       timestamptz NOT NULL,
                value        double precision,
                is_missing   boolean     NOT NULL
            )
            """
        ).format(schema=ident),
        sql.SQL(
            """
            CREATE INDEX IF NOT EXISTS observations_ingestion_idx
            ON {schema}.observations (ingestion_id)
            """
        ).format(schema=ident),
        # Latest ingestion per partition -> one clean series even after revisions.
        sql.SQL(
            """
            CREATE OR REPLACE VIEW {schema}.observations_current AS
            SELECT o.*
            FROM {schema}.observations o
            JOIN (
                SELECT DISTINCT ON (source, dataset, region, resolution, partition_date) id
                FROM {schema}.ingestion
                ORDER BY source, dataset, region, resolution, partition_date,
                         fetched_at DESC, id DESC
            ) latest ON latest.id = o.ingestion_id
            """
        ).format(schema=ident),
    ]


class RawZoneRepository:
    """Reads and writes the raw zone over an injected psycopg 3 connection.

    Injecting the connection keeps the repository trivially testable against a real
    Postgres service in CI while the Dagster resource / CLI own the connection lifecycle.
    """

    def __init__(self, conn: psycopg.Connection[tuple[Any, ...]], schema: str = "raw") -> None:
        self._conn = conn
        self._schema = schema

    @property
    def schema(self) -> str:
        return self._schema

    def ensure_schema(self) -> None:
        """Idempotently create the schema, tables, index, and view."""
        with self._conn.cursor() as cur:
            for statement in _ddl(self._schema):
                cur.execute(statement)
        self._conn.commit()

    def latest_hash(
        self, source: str, dataset: str, region: str, resolution: str, partition_date: date
    ) -> str | None:
        query = sql.SQL(
            """
            SELECT content_hash
            FROM {schema}.ingestion
            WHERE source = %s AND dataset = %s AND region = %s
              AND resolution = %s AND partition_date = %s
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """
        ).format(schema=sql.Identifier(self._schema))
        with self._conn.cursor() as cur:
            cur.execute(query, (source, dataset, region, resolution, partition_date))
            row = cur.fetchone()
        return None if row is None else str(row[0])

    def write_ingestion(self, record: IngestionRecord) -> WriteOutcome:
        """Persist an ingestion atomically; return whether it was a load, no-op, or revision.

        Uses ``ON CONFLICT DO NOTHING`` on the content key: if the exact content already
        exists nothing is written (a verifiable no-op). Any inserted row is either the
        partition's first load or -- if a differing hash already existed -- a revision.
        """
        schema = sql.Identifier(self._schema)
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                        INSERT INTO {schema}.ingestion (
                            source, dataset, region, resolution, partition_date,
                            source_tz, source_urls, payload, content_hash,
                            row_count, null_count, expected_count, hours_in_day,
                            dagster_run_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT
                            (source, dataset, region, resolution, partition_date, content_hash)
                        DO NOTHING
                        RETURNING id
                        """
                ).format(schema=schema),
                (
                    record.source,
                    record.dataset,
                    record.region,
                    record.resolution,
                    record.partition_date,
                    record.source_tz,
                    Jsonb(record.source_urls),
                    Jsonb(record.payload),
                    record.content_hash,
                    record.row_count,
                    record.null_count,
                    record.expected_count,
                    record.hours_in_day,
                    record.dagster_run_id,
                ),
            )
            inserted = cur.fetchone()
            if inserted is None:
                return WriteOutcome.NOOP

            ingestion_id = int(inserted[0])
            had_other_version = self._partition_has_other_version(cur, record, ingestion_id)
            self._insert_observations(cur, ingestion_id, record)

        return WriteOutcome.REVISION if had_other_version else WriteOutcome.LOADED

    def _partition_has_other_version(
        self,
        cur: psycopg.Cursor[tuple[Any, ...]],
        record: IngestionRecord,
        ingestion_id: int,
    ) -> bool:
        cur.execute(
            sql.SQL(
                """
                SELECT 1
                FROM {schema}.ingestion
                WHERE source = %s AND dataset = %s AND region = %s AND resolution = %s
                  AND partition_date = %s AND id <> %s
                LIMIT 1
                """
            ).format(schema=sql.Identifier(self._schema)),
            (
                record.source,
                record.dataset,
                record.region,
                record.resolution,
                record.partition_date,
                ingestion_id,
            ),
        )
        return cur.fetchone() is not None

    def _insert_observations(
        self,
        cur: psycopg.Cursor[tuple[Any, ...]],
        ingestion_id: int,
        record: IngestionRecord,
    ) -> None:
        rows = [
            (
                ingestion_id,
                record.dataset,
                record.region,
                record.resolution,
                _EPOCH + timedelta(milliseconds=ts_ms),
                value,
                value is None,
            )
            for ts_ms, value in record.payload
        ]
        if not rows:
            return
        cur.executemany(
            sql.SQL(
                """
                INSERT INTO {schema}.observations (
                    ingestion_id, dataset, region, resolution, ts_utc, value, is_missing
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
            ).format(schema=sql.Identifier(self._schema)),
            rows,
        )
