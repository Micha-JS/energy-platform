"""Partition definitions for market-data assets."""

from __future__ import annotations

from datetime import date

from dagster import DailyPartitionsDefinition

# Re-exported from a Dagster-free module so the CLI can read them without importing Dagster.
from energy_platform.orchestration.partition_config import (
    PARTITION_START,
    PARTITION_TIMEZONE,
)

__all__ = ["PARTITION_START", "PARTITION_TIMEZONE", "daily_de_partitions", "partition_key_to_date"]

daily_de_partitions = DailyPartitionsDefinition(
    start_date=PARTITION_START,
    timezone=PARTITION_TIMEZONE,
    fmt="%Y-%m-%d",
)


def partition_key_to_date(partition_key: str) -> date:
    return date.fromisoformat(partition_key)
