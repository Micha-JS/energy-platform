"""The publication ledger: what was told to the house, and when.

Retention already makes republishing an *overwrite* rather than an append -- a retained topic holds
exactly one message, so the duplicate-stream failure mode does not exist by construction. This
table is not there to prevent duplicates; it is there for two other things.

**A no-op has to be recognisable as one.** "Republishing an unchanged plan is a no-op" is only a
claim you can test if something remembers what the last payload was. The ledger stores the payload
digest (with ``published_at`` excluded, or nothing would ever match), so the publisher can say
*unchanged* instead of publishing identical bytes on every schedule tick and calling that
idempotent.

**An audit trail of advice.** This is the one component that speaks to the house. When someone asks
in three weeks why the battery was told to charge at 03:00 on the 14th, the answer needs a row
tying the message to the run that produced it -- which is what ``input_digest`` and the payload
digest are for. Cheap to keep, impossible to reconstruct later.

Replace-on-key, like the rest of ``derived``: one row per (site, day, tariff, topic), holding the
most recent publication rather than a history of attempts. The history that matters is the plan's,
and that lives in ``forward_dispatch_runs``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import psycopg
from psycopg import sql


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """What the ledger remembers about one published plan."""

    site_id: str
    local_date: date
    tariff_id: str
    topic: str
    payload_digest: str
    schema_version: int
    input_digest: str | None
    published_at: datetime


class PublicationRepository:
    """Reads and writes the publication ledger."""

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]], *, derived_schema: str):
        self._conn = conn
        self._derived = derived_schema

    def ensure_schema(self) -> None:
        ident = sql.Identifier(self._derived)
        statements = [
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=ident),
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {schema}.plan_publications (
                    site_id        text        NOT NULL,
                    local_date     date        NOT NULL,
                    tariff_id      text        NOT NULL,
                    topic          text        NOT NULL,
                    -- Over the payload with `published_at` removed: see PlanPayload.digest.
                    payload_digest text        NOT NULL,
                    schema_version integer     NOT NULL,
                    -- Copied off the run so a message can be tied to what produced it without a
                    -- join through a table that may since have been replaced.
                    input_digest   text,
                    published_at   timestamptz NOT NULL,
                    PRIMARY KEY (site_id, local_date, tariff_id, topic)
                )
                """
            ).format(schema=ident),
        ]
        with self._conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
        self._conn.commit()

    def last_digest(self, site_id: str, day: date, tariff_id: str, topic: str) -> str | None:
        """The digest last published on this topic for this plan, if any."""
        query = sql.SQL(
            """
            SELECT payload_digest FROM {table}
            WHERE site_id = %s AND local_date = %s AND tariff_id = %s AND topic = %s
            """
        ).format(table=sql.Identifier(self._derived, "plan_publications"))
        with self._conn.cursor() as cur:
            cur.execute(query, (site_id, day, tariff_id, topic))
            row = cur.fetchone()
        return None if row is None else str(row[0])

    def record(self, publication: PublicationRecord) -> None:
        query = sql.SQL(
            """
            INSERT INTO {table} (
                site_id, local_date, tariff_id, topic,
                payload_digest, schema_version, input_digest, published_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (site_id, local_date, tariff_id, topic) DO UPDATE SET
                payload_digest = EXCLUDED.payload_digest,
                schema_version = EXCLUDED.schema_version,
                input_digest   = EXCLUDED.input_digest,
                published_at   = EXCLUDED.published_at
            """
        ).format(table=sql.Identifier(self._derived, "plan_publications"))
        with self._conn.cursor() as cur:
            cur.execute(
                query,
                (
                    publication.site_id,
                    publication.local_date,
                    publication.tariff_id,
                    publication.topic,
                    publication.payload_digest,
                    publication.schema_version,
                    publication.input_digest,
                    publication.published_at,
                ),
            )
        self._conn.commit()
