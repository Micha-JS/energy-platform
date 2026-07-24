"""A daily schedule that keeps the market-data partitions fresh."""

from __future__ import annotations

from dagster import (
    DefaultScheduleStatus,
    build_schedule_from_partitioned_job,
    define_asset_job,
)

from energy_platform.orchestration.assets import market_data_assets

market_data_job = define_asset_job(
    name="market_data_job",
    selection=market_data_assets,
)

# Runs once per Europe/Berlin day (partition timezone), materialising the day that just
# closed -- by which time both day-ahead prices and grid load are published.
daily_market_data_schedule = build_schedule_from_partitioned_job(
    market_data_job,
    hour_of_day=6,
    minute_of_hour=0,
    # Ships enabled so a fresh deploy keeps partitions fresh without a manual UI toggle.
    default_status=DefaultScheduleStatus.RUNNING,
)
