"""Daily schedules that keep the market-data and weather partitions fresh."""

from __future__ import annotations

from datetime import timedelta

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


# -- Weather actuals: settled truth, but ERA5 settles late -----------------------------

weather_actuals_job = define_asset_job(
    name="weather_actuals_job",
    selection=[open_meteo_weather_actuals_raw],
)

# The Open-Meteo archive is ERA5-backed and only finalises a few days after the fact, so a
# schedule chasing yesterday (D-1) would perpetually fetch data that doesn't exist yet and
# leave failing/empty partitions until a manual re-run. Lag the target day past that horizon;
# missed days in between are re-materialised when their lag elapses (or by a manual backfill).
ARCHIVE_LAG_DAYS = 5


@schedule(
    job=weather_actuals_job,
    cron_schedule="30 6 * * *",
    execution_timezone=PARTITION_TIMEZONE,
    default_status=DefaultScheduleStatus.RUNNING,
)
def daily_weather_actuals_schedule(context: ScheduleEvaluationContext) -> RunRequest:
    """Materialise the most recent Berlin day the ERA5 archive is expected to have settled."""
    target_day = context.scheduled_execution_time.date() - timedelta(days=ARCHIVE_LAG_DAYS)
    return RunRequest(partition_key=target_day.isoformat())


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
