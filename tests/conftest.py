"""Shared test fixtures.

The SMARD and Open-Meteo clients are exercised entirely offline against recorded fixtures via
the shared ``httpx.MockTransport`` in :mod:`energy_platform.connectors.offline` -- CI makes no
live API calls, and tests route through the same fixture-name rules the CLI's ``--offline`` mode
uses. The Postgres fixtures connect to a real database (a service container in CI) and skip
cleanly when none is available.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest

from energy_platform.config import PostgresConfig
from energy_platform.connectors.offline import offline_transport
from energy_platform.connectors.open_meteo import OpenMeteoArchiveClient, OpenMeteoForecastClient
from energy_platform.connectors.smard import SmardClient
from energy_platform.orchestration.raw_zone import RawZoneRepository

FIXTURES_DIR = Path(__file__).parent / "connectors" / "fixtures"

# The site the offline weather fixtures were recorded for (Berlin, rounded to 2 dp).
FIXTURE_COORDINATES = {"home": (52.52, 13.40)}


@pytest.fixture
def smard_client() -> Iterator[SmardClient]:
    """A :class:`SmardClient` served from recorded fixtures (no network, no retries)."""
    with httpx.Client(transport=offline_transport(FIXTURES_DIR)) as http:
        yield SmardClient(http, max_retries=0, sleep=lambda _: None)


@pytest.fixture
def open_meteo_archive_client() -> Iterator[OpenMeteoArchiveClient]:
    """An archive client served from offline fixtures (no network, no retries)."""
    with httpx.Client(transport=offline_transport(FIXTURES_DIR)) as http:
        yield OpenMeteoArchiveClient(http, FIXTURE_COORDINATES, max_retries=0, sleep=lambda _: None)


@pytest.fixture
def open_meteo_forecast_client() -> Iterator[OpenMeteoForecastClient]:
    """A forecast client served from offline fixtures (no network, no retries)."""
    with httpx.Client(transport=offline_transport(FIXTURES_DIR)) as http:
        yield OpenMeteoForecastClient(
            http,
            FIXTURE_COORDINATES,
            forecast_days=7,
            past_days=1,
            max_retries=0,
            sleep=lambda _: None,
        )


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
