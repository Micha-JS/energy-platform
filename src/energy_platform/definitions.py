"""Dagster code location entrypoint.

Composes the market-data and weather assets, their daily schedules, and the SMARD /
Open-Meteo / raw-zone resources (configured from the environment) into the code location the
webserver and daemon load. ``workspace.yaml`` / ``dagster.yaml`` point at the ``defs``
attribute and need no changes as milestones accrete.
"""

from dagster import Definitions, EnvVar

from energy_platform.config import AppConfig
from energy_platform.orchestration.assets import (
    forecasting_assets,
    market_data_assets,
    telemetry_assets,
    weather_assets,
)
from energy_platform.orchestration.resources import (
    ForecastPostgresResource,
    HomeAssistantResource,
    OpenMeteoArchiveClientResource,
    OpenMeteoForecastClientResource,
    RawZonePostgresResource,
    SmardClientResource,
    SyntheticTelemetryResource,
)
from energy_platform.orchestration.schedules import (
    daily_market_data_schedule,
    daily_telemetry_schedule,
    daily_weather_actuals_schedule,
    daily_weather_forecast_schedule,
)

_config = AppConfig.from_env()

# site id -> [lat, lon] for the Dagster-config-friendly resource fields (Dagster config wants
# JSON-native lists, so widen the shared (lat, lon) tuples to lists here).
_coordinates = {site_id: [lat, lon] for site_id, (lat, lon) in _config.site.coordinates.items()}

defs = Definitions(
    assets=[*market_data_assets, *weather_assets, *telemetry_assets, *forecasting_assets],
    schedules=[
        daily_market_data_schedule,
        daily_weather_actuals_schedule,
        daily_weather_forecast_schedule,
        daily_telemetry_schedule,
    ],
    resources={
        "forecast_store": ForecastPostgresResource(
            dsn=_config.postgres.dsn,
            derived_schema=_config.postgres.derived_schema,
            marts_schema=_config.postgres.marts_schema,
            staging_schema=_config.postgres.staging_schema,
        ),
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
        "synthetic_telemetry": SyntheticTelemetryResource(
            pv_dc_kwp=_config.pv.dc_kwp,
            pv_ac_cap_kw=_config.pv.ac_cap_kw,
            pv_performance_ratio=_config.pv.performance_ratio,
            pv_temp_coeff_per_c=_config.pv.temp_coeff_per_c,
            battery_capacity_kwh=_config.battery.capacity_kwh,
            battery_soc_min=_config.battery.soc_min,
            battery_soc_max=_config.battery.soc_max,
            battery_max_charge_kw=_config.battery.max_charge_kw,
            battery_max_discharge_kw=_config.battery.max_discharge_kw,
            battery_round_trip_efficiency=_config.battery.round_trip_efficiency,
            synthetic_salt=_config.synthetic.salt,
            synthetic_annual_load_kwh=_config.synthetic.annual_load_kwh,
            thermal_r_k_per_kw=_config.thermal.r_k_per_kw,
            thermal_c_kwh_per_k=_config.thermal.c_kwh_per_k,
            thermal_solar_gain_kw_per_wm2=_config.thermal.solar_gain_kw_per_wm2,
            thermostat_setpoint_c=_config.thermal.setpoint_c,
            thermostat_deadband_k=_config.thermal.deadband_k,
            heating_setpoint_c=_config.thermal.heating_setpoint_c,
            ac_cop=_config.thermal.cop,
            ac_rated_kw=_config.thermal.rated_kw,
            default_site_id=_config.site.default_id,
        ),
        "home_assistant": HomeAssistantResource(
            enabled=_config.home_assistant.enabled,
            base_url=_config.home_assistant.base_url,
            # Resolve the token at run time from the environment rather than baking its value into
            # the resource config -- otherwise Dagster serialises the long-lived token into run
            # storage and renders it in the launchpad UI. EnvVar keeps only the var *name* on disk.
            token=EnvVar("ENERGY_HA_TOKEN"),
            verify_tls=_config.home_assistant.verify_tls,
            entity_map=_config.home_assistant.entities,
            default_site_id=_config.site.default_id,
        ),
        "raw_zone": RawZonePostgresResource(
            dsn=_config.postgres.dsn,
            schema_name=_config.postgres.schema,
        ),
    },
)
