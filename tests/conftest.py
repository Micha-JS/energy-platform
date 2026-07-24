"""Shared test fixtures.

The SMARD client is exercised entirely offline against recorded fixtures via an
``httpx.MockTransport`` -- CI makes no live API calls. The Postgres fixtures connect to a
real database (a service container in CI) and skip cleanly when none is available.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest

from energy_platform.config import PostgresConfig
from energy_platform.connectors.smard import SmardClient
from energy_platform.orchestration.raw_zone import RawZoneRepository

FIXTURES_DIR = Path(__file__).parent / "connectors" / "fixtures"


def _fixture_name(url: httpx.URL) -> str:
    """Map a SMARD request URL to its recorded fixture filename.

    ``.../{filter}/{region}/index_{res}.json`` -> ``{filter}_{region}_index_{res}.json``;
    weekly files already embed ``{filter}_{region}_{res}_{ts}`` and are used as-is.
    """
    parts = url.path.rstrip("/").split("/")
    filter_id, region, filename = parts[-3], parts[-2], parts[-1]
    if filename.startswith("index_"):
        return f"{filter_id}_{region}_{filename}"
    return filename


def smard_mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = FIXTURES_DIR / _fixture_name(request.url)
        if not path.exists():
            return httpx.Response(404, text=f"no fixture: {path.name}")
        return httpx.Response(
            200,
            content=path.read_bytes(),
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def smard_client() -> Iterator[SmardClient]:
    """A :class:`SmardClient` served from recorded fixtures (no network, no retries)."""
    with httpx.Client(transport=smard_mock_transport()) as http:
        yield SmardClient(http, max_retries=0, sleep=lambda _: None)


@pytest.fixture
def postgres_conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """A live Postgres connection.

    Skips locally when no database is reachable, so the suite stays runnable without one.
    In an environment that provisions Postgres (CI sets ``ENERGY_REQUIRE_POSTGRES=1``) an
    unreachable database is a hard failure instead -- otherwise a broken service/env would
    silently skip the raw-zone idempotency tests and keep the build green.
    """
    config = PostgresConfig.from_env()
    try:
        conn = psycopg.connect(config.dsn, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("ENERGY_REQUIRE_POSTGRES"):
            pytest.fail(f"ENERGY_REQUIRE_POSTGRES is set but no Postgres is reachable ({exc})")
        pytest.skip(f"no Postgres available ({exc})")
    with conn:
        yield conn


@pytest.fixture
def raw_repo(
    postgres_conn: psycopg.Connection[tuple[object, ...]],
) -> Iterator[RawZoneRepository]:
    """A repository bound to a throwaway schema, dropped on teardown."""
    schema = "raw_test"
    with postgres_conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    postgres_conn.commit()

    repo = RawZoneRepository(postgres_conn, schema=schema)
    repo.ensure_schema()
    try:
        yield repo
    finally:
        with postgres_conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        postgres_conn.commit()
