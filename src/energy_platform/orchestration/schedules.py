"""Daily schedules that keep the market-data and weather partitions fresh."""

from __future__ import annotations

from dagster import (
    DefaultScheduleStatus,
    RunRequest,
    ScheduleEvaluationContext,
    build_schedule_from_partitioned_job,
    define_asset_job,
    schedule,
)

from energy_platform.orchestration.assets import (
    market_data_assets,
    open_meteo_weather_actuals_raw,
    open_meteo_weather_forecast_raw,
)
from energy_platform.orchestration.partition_config import PARTITION_TIMEZONE

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


# -- Weather actuals: settled truth, so materialise the day that just closed -----------

weather_actuals_job = define_asset_job(
    name="weather_actuals_job",
    selection=[open_meteo_weather_actuals_raw],
)

daily_weather_actuals_schedule = build_schedule_from_partitioned_job(
    weather_actuals_job,
    hour_of_day=6,
    minute_of_hour=30,
    default_status=DefaultScheduleStatus.RUNNING,
)


# -- Weather forecast: a vintage of the future, so materialise TODAY's issue date ------

weather_forecast_job = define_asset_job(
    name="weather_forecast_job",
    selection=[open_meteo_weather_forecast_raw],
)


@schedule(
    job=weather_forecast_job,
    cron_schedule="0 7 * * *",
    execution_timezone=PARTITION_TIMEZONE,
    default_status=DefaultScheduleStatus.RUNNING,
)
def daily_weather_forecast_schedule(context: ScheduleEvaluationContext) -> RunRequest:
    """Capture today's forecast vintage.

    Unlike settled data, a forecast is about the future, so we materialise the *current* Berlin
    day's partition (the issue date) rather than the day that just closed. ``end_offset=1`` on
    ``daily_forecast_partitions`` makes today a valid partition key.
    """
    issue_day = context.scheduled_execution_time.date()
    return RunRequest(partition_key=issue_day.isoformat())
