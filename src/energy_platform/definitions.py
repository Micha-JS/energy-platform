"""Dagster code location entrypoint.

Composes the M1 market-data assets, their daily schedule, and the SMARD/raw-zone
resources (configured from the environment) into the code location the webserver and
daemon load. ``workspace.yaml`` / ``dagster.yaml`` point at the ``defs`` attribute and
need no changes as milestones accrete.
"""

from dagster import Definitions

from energy_platform.config import AppConfig
from energy_platform.orchestration.assets import market_data_assets
from energy_platform.orchestration.resources import (
    RawZonePostgresResource,
    SmardClientResource,
)
from energy_platform.orchestration.schedules import daily_market_data_schedule

_config = AppConfig.from_env()

defs = Definitions(
    assets=market_data_assets,
    schedules=[daily_market_data_schedule],
    resources={
        "smard": SmardClientResource(
            base_url=_config.smard.base_url,
            timeout_seconds=_config.smard.timeout_seconds,
            max_retries=_config.smard.max_retries,
        ),
        "raw_zone": RawZonePostgresResource(
            dsn=_config.postgres.dsn,
            schema_name=_config.postgres.schema,
        ),
    },
)
