"""Dagster code location entrypoint.

Composes the market-data and weather assets, their daily schedules, and the SMARD /
Open-Meteo / raw-zone resources (configured from the environment) into the code location the
webserver and daemon load. ``workspace.yaml`` / ``dagster.yaml`` point at the ``defs``
attribute and need no changes as milestones accrete.
"""

from dagster import Definitions

from energy_platform.config import AppConfig
from energy_platform.orchestration.assets import market_data_assets, weather_assets
from energy_platform.orchestration.resources import (
    OpenMeteoArchiveClientResource,
    OpenMeteoForecastClientResource,
    RawZonePostgresResource,
    SmardClientResource,
)
from energy_platform.orchestration.schedules import (
    daily_market_data_schedule,
    daily_weather_actuals_schedule,
    daily_weather_forecast_schedule,
)

_config = AppConfig.from_env()

# site id -> [lat, lon] for the Dagster-config-friendly resource fields.
_coordinates = {site.id: [site.latitude, site.longitude] for site in _config.site.sites}

defs = Definitions(
    assets=[*market_data_assets, *weather_assets],
    schedules=[
        daily_market_data_schedule,
        daily_weather_actuals_schedule,
        daily_weather_forecast_schedule,
    ],
    resources={
        "smard": SmardClientResource(
            base_url=_config.smard.base_url,
            timeout_seconds=_config.smard.timeout_seconds,
            max_retries=_config.smard.max_retries,
        ),
        "open_meteo_archive": OpenMeteoArchiveClientResource(
            base_url=_config.open_meteo.archive_url,
            timeout_seconds=_config.open_meteo.timeout_seconds,
            max_retries=_config.open_meteo.max_retries,
            default_site_id=_config.site.default_id,
            coordinates=_coordinates,
        ),
        "open_meteo_forecast": OpenMeteoForecastClientResource(
            base_url=_config.open_meteo.forecast_url,
            timeout_seconds=_config.open_meteo.timeout_seconds,
            max_retries=_config.open_meteo.max_retries,
            forecast_days=_config.open_meteo.forecast_days,
            default_site_id=_config.site.default_id,
            coordinates=_coordinates,
        ),
        "raw_zone": RawZonePostgresResource(
            dsn=_config.postgres.dsn,
            schema_name=_config.postgres.schema,
        ),
    },
)
