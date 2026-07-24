"""Dagster resources wrapping the SMARD client and the raw-zone repository."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import psycopg
from dagster import ConfigurableResource, InitResourceContext

from energy_platform.config import DEFAULT_RAW_SCHEMA, DEFAULT_SMARD_BASE_URL
from energy_platform.connectors.smard import USER_AGENT, SmardClient
from energy_platform.orchestration.raw_zone import RawZoneRepository


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
