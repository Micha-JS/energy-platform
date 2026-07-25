"""Dagster resources wrapping the SMARD / Open-Meteo clients and the raw-zone repository."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import psycopg
from dagster import ConfigurableResource, InitResourceContext

from energy_platform.config import (
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_OPEN_METEO_ARCHIVE_URL,
    DEFAULT_OPEN_METEO_FORECAST_URL,
    DEFAULT_PV_AC_CAP_KW,
    DEFAULT_PV_DC_KWP,
    DEFAULT_PV_PERFORMANCE_RATIO,
    DEFAULT_PV_TEMP_COEFF_PER_C,
    DEFAULT_RAW_SCHEMA,
    DEFAULT_SITE_ID,
    DEFAULT_SMARD_BASE_URL,
    BatteryConfig,
    PvSystemConfig,
    SyntheticConfig,
)
from energy_platform.connectors.home_assistant import USER_AGENT as HA_USER_AGENT
from energy_platform.connectors.home_assistant import HomeAssistantClient, HomeAssistantError
from energy_platform.connectors.open_meteo import USER_AGENT as OPEN_METEO_USER_AGENT
from energy_platform.connectors.open_meteo import (
    OpenMeteoArchiveClient,
    OpenMeteoForecastClient,
)
from energy_platform.connectors.smard import USER_AGENT, SmardClient
from energy_platform.connectors.synthetic import (
    WEATHER_SOURCE,
    SyntheticTelemetryClient,
    WeatherReader,
)
from energy_platform.connectors.types import Dataset
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


class SyntheticTelemetryResource(ConfigurableResource[SyntheticTelemetryClient]):
    """Builds a :class:`SyntheticTelemetryClient` from the shared PV / battery / synthetic config.

    The client needs a :class:`WeatherReader` to pull already-ingested irradiance, so the client
    is built from the run's raw-zone repository via :meth:`build` -- the same repository the
    asset writes telemetry back through, so read and write share one connection.
    """

    pv_dc_kwp: float = DEFAULT_PV_DC_KWP
    pv_ac_cap_kw: float = DEFAULT_PV_AC_CAP_KW
    pv_performance_ratio: float = DEFAULT_PV_PERFORMANCE_RATIO
    pv_temp_coeff_per_c: float = DEFAULT_PV_TEMP_COEFF_PER_C
    battery_capacity_kwh: float = DEFAULT_BATTERY_CAPACITY_KWH
    battery_soc_min: float = 0.05
    battery_soc_max: float = 1.0
    battery_max_charge_kw: float = 5.0
    battery_max_discharge_kw: float = 5.0
    battery_round_trip_efficiency: float = 0.90
    synthetic_salt: str = "energy-platform-synthetic-v1"
    synthetic_annual_load_kwh: float = 4000.0
    weather_source: str = WEATHER_SOURCE
    default_site_id: str = DEFAULT_SITE_ID

    def build(self, reader: WeatherReader) -> SyntheticTelemetryClient:
        return SyntheticTelemetryClient(
            reader,
            pv=PvSystemConfig(
                dc_kwp=self.pv_dc_kwp,
                ac_cap_kw=self.pv_ac_cap_kw,
                performance_ratio=self.pv_performance_ratio,
                temp_coeff_per_c=self.pv_temp_coeff_per_c,
            ),
            battery=BatteryConfig(
                capacity_kwh=self.battery_capacity_kwh,
                soc_min=self.battery_soc_min,
                soc_max=self.battery_soc_max,
                max_charge_kw=self.battery_max_charge_kw,
                max_discharge_kw=self.battery_max_discharge_kw,
                round_trip_efficiency=self.battery_round_trip_efficiency,
            ),
            synthetic=SyntheticConfig(
                salt=self.synthetic_salt,
                annual_load_kwh=self.synthetic_annual_load_kwh,
            ),
            weather_source=self.weather_source,
        )


class HomeAssistantResource(ConfigurableResource[HomeAssistantClient]):
    """Builds a read-only :class:`HomeAssistantClient` for real Fenecon telemetry.

    Disabled by default. The base URL, token, and entity map come from the environment (never
    committed), so the public repo and CI can never reach a live house. :meth:`get_client`
    refuses to build a client unless ``enabled`` is set with a base URL and token present.
    """

    enabled: bool = False
    base_url: str = ""
    token: str = ""
    verify_tls: bool = True
    # Required (always supplied from config); avoids a mutable class-level default.
    entity_map: dict[str, str]
    timeout_seconds: float = 30.0
    max_retries: int = 3
    default_site_id: str = DEFAULT_SITE_ID

    @contextmanager
    def get_client(self) -> Iterator[HomeAssistantClient]:
        if not self.enabled:
            raise HomeAssistantError(
                "Home Assistant connector is disabled; set ENERGY_HA_ENABLED=1 with "
                "ENERGY_HA_URL and ENERGY_HA_TOKEN to enable real telemetry ingestion."
            )
        if not self.base_url or not self.token:
            raise HomeAssistantError(
                "ENERGY_HA_URL and ENERGY_HA_TOKEN must both be set when the connector is enabled."
            )
        entities = {Dataset(key): value for key, value in self.entity_map.items()}
        with httpx.Client(
            timeout=self.timeout_seconds,
            verify=self.verify_tls,
            headers={
                "User-Agent": HA_USER_AGENT,
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        ) as http:
            yield HomeAssistantClient(
                http,
                self.base_url,
                entities,
                max_retries=self.max_retries,
            )
