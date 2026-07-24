"""Partition constants, free of any Dagster import.

Kept separate from :mod:`energy_platform.orchestration.partitions` so lightweight consumers
-- notably the backfill CLI, which runs directly against Postgres with no Dagster instance --
can read the partition calendar without paying Dagster's import cost.
"""

from __future__ import annotations

from typing import Final

# Full available SMARD history. Partition keys are Europe/Berlin calendar days, matching
# the day-ahead market's notion of a day (23/24/25 hours across DST). Weather actuals reuse
# this calendar so weather backfills alongside prices over the same range.
PARTITION_START: Final = "2015-01-01"
PARTITION_TIMEZONE: Final = "Europe/Berlin"

# Forecast vintages can only accrue *forward*: the forecast API serves the current issue, so
# past issue dates are un-materialisable. The calendar therefore starts recently, where
# vintages can actually be captured -- not at the deep historical start above.
FORECAST_PARTITION_START: Final = "2026-01-01"
