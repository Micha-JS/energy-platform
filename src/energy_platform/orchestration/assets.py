"""Daily-partitioned Dagster assets for SMARD market data.

Both assets are thin wrappers over :func:`ingest_partition`; the CLI drives the same core,
so a Dagster backfill over a range the CLI already loaded shows green partitions and is a
proven no-op (matching content hashes).
"""
# NB: no ``from __future__ import annotations`` here -- Dagster resolves the ``context``
# parameter annotation at runtime, and stringized annotations break inside the factory.

from dagster import (
    AssetExecutionContext,
    AssetsDefinition,
    MaterializeResult,
    asset,
)

from energy_platform.connectors.types import Dataset, Resolution
from energy_platform.orchestration.ingest import ingest_partition
from energy_platform.orchestration.partitions import (
    daily_de_partitions,
    partition_key_to_date,
)
from energy_platform.orchestration.resources import (
    RawZonePostgresResource,
    SmardClientResource,
)

REGION = "DE"
# M1 materialises hourly series; the client also supports quarter-hour (tested), which a
# future asset can request without any change to the ingestion core.
RESOLUTION = Resolution.HOUR


def _build_asset(dataset: Dataset, name: str, description: str) -> AssetsDefinition:
    @asset(
        name=name,
        description=description,
        partitions_def=daily_de_partitions,
        group_name="market_data",
        kinds={"python", "postgres"},
    )
    def _market_data_asset(
        context: AssetExecutionContext,
        smard: SmardClientResource,
        raw_zone: RawZonePostgresResource,
    ) -> MaterializeResult:
        day = partition_key_to_date(context.partition_key)
        # Schema setup runs once per process in RawZonePostgresResource.setup_for_execution,
        # so the per-partition path only ingests.
        with raw_zone.get_repository() as repo, smard.get_client() as client:
            result = ingest_partition(
                client,
                repo,
                dataset,
                REGION,
                RESOLUTION,
                day,
                dagster_run_id=context.run_id,
            )

        context.log.info(
            "%s %s: %s (%d/%d rows, %d null, %dh)",
            name,
            day.isoformat(),
            result.outcome.value,
            result.row_count,
            result.expected_count,
            result.null_count,
            result.hours_in_day,
        )
        return MaterializeResult(
            metadata={
                "outcome": result.outcome.value,
                "is_noop": result.is_noop,
                "content_hash": result.content_hash,
                "row_count": result.row_count,
                "expected_count": result.expected_count,
                "null_count": result.null_count,
                "has_missing": result.has_missing,
                "hours_in_day": result.hours_in_day,
            }
        )

    return _market_data_asset


smard_day_ahead_price_raw = _build_asset(
    Dataset.DAY_AHEAD_PRICE,
    "smard_day_ahead_price_raw",
    "SMARD German day-ahead wholesale electricity price (EUR/MWh), hourly, "
    "one Europe/Berlin day per partition, stored append-only in the raw zone.",
)

smard_grid_load_raw = _build_asset(
    Dataset.GRID_LOAD,
    "smard_grid_load_raw",
    "SMARD German grid load / total consumption (MW), hourly, one Europe/Berlin day "
    "per partition, stored append-only in the raw zone.",
)

market_data_assets = [smard_day_ahead_price_raw, smard_grid_load_raw]
