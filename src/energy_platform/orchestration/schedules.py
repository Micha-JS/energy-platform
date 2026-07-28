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

from energy_platform.config import ARCHIVE_LAG_DAYS
from energy_platform.orchestration.assets import (
    market_data_assets,
    open_meteo_weather_actuals_raw,
    open_meteo_weather_forecast_raw,
    published_plan,
    synthetic_telemetry_raw,
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
#
# The constant itself moved to energy_platform.config at M7: the backtester needs it as the
# *observation lag* on synthetic telemetry and cannot import this module without importing Dagster.
# One definition, two readers -- the same rule the coverage windows follow.


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


# -- Telemetry: synthetic, derived from weather, so it trails the weather lag ----------

telemetry_job = define_asset_job(
    name="telemetry_job",
    selection=[synthetic_telemetry_raw],
)


@schedule(
    job=telemetry_job,
    # An hour after the weather-actuals schedule (06:30) so the same target day's irradiance has
    # landed before telemetry derives PV from it.
    cron_schedule="30 7 * * *",
    execution_timezone=PARTITION_TIMEZONE,
    default_status=DefaultScheduleStatus.RUNNING,
)
def daily_telemetry_schedule(context: ScheduleEvaluationContext) -> RunRequest:
    """Materialise synthetic telemetry for the same settled day the weather schedule targets.

    Telemetry derives PV from ingested irradiance, so it must trail the ERA5 archive lag by the
    same margin as weather actuals -- otherwise it would generate from irradiance that has not
    settled yet. Only the synthetic asset is scheduled; the real Fenecon asset is manual.
    """
    target_day = context.scheduled_execution_time.date() - timedelta(days=ARCHIVE_LAG_DAYS)
    return RunRequest(partition_key=target_day.isoformat())


# -- Publishing: the one schedule that speaks OUT of the platform ----------------------

publish_plan_job = define_asset_job(
    name="publish_plan_job",
    selection=[published_plan],
)


@schedule(
    job=publish_plan_job,
    # 00:15 Europe/Berlin: after the decision time the plan is defined at (DECISION_RULE_ID is
    # "berlin_midnight_before_target_day"), and early enough that a house has the day's schedule
    # before the first hour of it is over. Publishing before midnight would advertise a plan for a
    # day that, by the platform's own decision rule, has not been decided yet.
    cron_schedule="15 0 * * *",
    execution_timezone=PARTITION_TIMEZONE,
    # STOPPED, and the only schedule here that is -- every other one accrues data into the
    # platform's own storage, where the cost of an unwanted run is a wasted API call. This one
    # transmits a recommendation to a household's automation system. A fresh deploy that started
    # broadcasting because nobody thought to look at the schedules page would be a genuinely bad
    # surprise, so enabling it is a deliberate act. Same reasoning that leaves the real Fenecon
    # asset defined but unscheduled.
    default_status=DefaultScheduleStatus.STOPPED,
)
def daily_publish_plan_schedule(context: ScheduleEvaluationContext) -> RunRequest:
    """Publish the plan for the Berlin day that has just begun.

    Unpartitioned: the asset publishes *the current plan*, and the broker keeps exactly one
    retained message per topic. The history lives in derived.plan_publications and in the
    forward-dispatch tables, not in a partition per day.
    """
    del context  # the asset resolves today itself, from the same partition timezone
    return RunRequest()
