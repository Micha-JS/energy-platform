"""Partition constants, free of any Dagster import.

Kept separate from :mod:`energy_platform.orchestration.partitions` so lightweight consumers
-- notably the backfill CLI, which runs directly against Postgres with no Dagster instance --
can read the partition calendar without paying Dagster's import cost.
"""

from __future__ import annotations

from typing import Final

# Full available SMARD history. Partition keys are Europe/Berlin calendar days, matching
# the day-ahead market's notion of a day (23/24/25 hours across DST).
PARTITION_START: Final = "2015-01-01"
PARTITION_TIMEZONE: Final = "Europe/Berlin"
