"""Dagster resources wrapping the SMARD / Open-Meteo clients and the raw-zone repository."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import psycopg
from dagster import ConfigurableResource, InitResourceContext

from energy_platform.config import (
    DEFAULT_OPEN_METEO_ARCHIVE_URL,
    DEFAULT_OPEN_METEO_FORECAST_URL,
    DEFAULT_RAW_SCHEMA,
    DEFAULT_SITE_ID,
    DEFAULT_SMARD_BASE_URL,
)
from energy_platform.connectors.open_meteo import USER_AGENT as OPEN_METEO_USER_AGENT
from energy_platform.connectors.open_meteo import (
    OpenMeteoArchiveClient,
    OpenMeteoForecastClient,
)
from energy_platform.connectors.smard import USER_AGENT, SmardClient
from energy_platform.orchestration.raw_zone import RawZoneRepository


def _coord_map(coordinates: dict[str, list[float]]) -> dict[str, tuple[float, float]]:
    """Dagster config carries coordinates as ``[lat, lon]`` lists; the clients want tuples."""
    return {site_id: (pair[0], pair[1]) for site_id, pair in coordinates.items()}


class SmardClientResource(ConfigurableResource[SmardClient]):
    """Provides a configured :class:`SmardClient` for the run's lifetime."""

    base_url: str = DEFAULT_SMARD_BASE_URL
    timeout_seconds: float = 30.0
    max_retries: int = 3

    @contextmanager
    def get_client(self) -> Iterator[SmardClient]:
        with httpx.Client(
            timeout=self.timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        ) as http:
            yield SmardClient(http, base_url=self.base_url, max_retries=self.max_retries)


class RawZonePostgresResource(ConfigurableResource[RawZoneRepository]):
    """Opens a psycopg 3 connection to the raw zone for the run's lifetime."""

    dsn: str
    schema_name: str = DEFAULT_RAW_SCHEMA

    def setup_for_execution(self, context: InitResourceContext) -> None:
        # Create the schema/tables/view once per run process, not on the per-partition hot
        # path -- a large backfill would otherwise re-issue the DDL for every partition.
        with self.get_repository() as repo:
            repo.ensure_schema()

    @contextmanager
    def get_repository(self) -> Iterator[RawZoneRepository]:
        with psycopg.connect(self.dsn) as conn:
            yield RawZoneRepository(conn, schema=self.schema_name)


class OpenMeteoArchiveClientResource(ConfigurableResource[OpenMeteoArchiveClient]):
    """Provides a configured :class:`OpenMeteoArchiveClient` for historical weather actuals.

    ``coordinates`` maps each site id to ``[latitude, longitude]``; ``default_site_id`` is the
    site weather assets materialise for.
    """

    base_url: str = DEFAULT_OPEN_METEO_ARCHIVE_URL
    timeout_seconds: float = 30.0
    max_retries: int = 3
    default_site_id: str = DEFAULT_SITE_ID
    coordinates: dict[str, list[float]]

    @contextmanager
    def get_client(self) -> Iterator[OpenMeteoArchiveClient]:
        with httpx.Client(
            timeout=self.timeout_seconds,
            headers={"User-Agent": OPEN_METEO_USER_AGENT, "Accept": "application/json"},
        ) as http:
            yield OpenMeteoArchiveClient(
                http,
                _coord_map(self.coordinates),
                base_url=self.base_url,
                max_retries=self.max_retries,
            )


class OpenMeteoForecastClientResource(ConfigurableResource[OpenMeteoForecastClient]):
    """Provides a configured :class:`OpenMeteoForecastClient` for forecast vintages."""

    base_url: str = DEFAULT_OPEN_METEO_FORECAST_URL
    timeout_seconds: float = 30.0
    max_retries: int = 3
    forecast_days: int = 7
    past_days: int = 1
    default_site_id: str = DEFAULT_SITE_ID
    coordinates: dict[str, list[float]]

    @contextmanager
    def get_client(self) -> Iterator[OpenMeteoForecastClient]:
        with httpx.Client(
            timeout=self.timeout_seconds,
            headers={"User-Agent": OPEN_METEO_USER_AGENT, "Accept": "application/json"},
        ) as http:
            yield OpenMeteoForecastClient(
                http,
                _coord_map(self.coordinates),
                base_url=self.base_url,
                forecast_days=self.forecast_days,
                past_days=self.past_days,
                max_retries=self.max_retries,
            )
