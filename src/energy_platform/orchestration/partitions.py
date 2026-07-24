"""Partition definitions for market-data assets."""

from __future__ import annotations

from datetime import date

from dagster import DailyPartitionsDefinition

# Re-exported from a Dagster-free module so the CLI can read them without importing Dagster.
from energy_platform.orchestration.partition_config import (
    FORECAST_PARTITION_START,
    PARTITION_START,
    PARTITION_TIMEZONE,
)

__all__ = [
    "FORECAST_PARTITION_START",
    "PARTITION_START",
    "PARTITION_TIMEZONE",
    "daily_de_partitions",
    "daily_forecast_partitions",
    "daily_weather_partitions",
    "partition_key_to_date",
]

daily_de_partitions = DailyPartitionsDefinition(
    start_date=PARTITION_START,
    timezone=PARTITION_TIMEZONE,
    fmt="%Y-%m-%d",
)

# Weather actuals: same Berlin-day calendar and range as the market data they sit alongside.
daily_weather_partitions = DailyPartitionsDefinition(
    start_date=PARTITION_START,
    timezone=PARTITION_TIMEZONE,
    fmt="%Y-%m-%d",
)

# Forecast vintages, keyed by issue date. ``end_offset=1`` extends the calendar through *today*
# so the current issue date is a materialisable partition (a forecast is about the future, so
# the schedule captures today's issue, not yesterday's closed day).
daily_forecast_partitions = DailyPartitionsDefinition(
    start_date=FORECAST_PARTITION_START,
    timezone=PARTITION_TIMEZONE,
    end_offset=1,
    fmt="%Y-%m-%d",
)


def partition_key_to_date(partition_key: str) -> date:
    return date.fromisoformat(partition_key)
