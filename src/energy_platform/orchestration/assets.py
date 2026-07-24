"""Daily-partitioned Dagster assets for SMARD market data and Open-Meteo weather.

Every asset is a thin wrapper over the shared ingestion core (:func:`ingest_partition` for
settled series, :func:`ingest_forecast_vintage` for forecast vintages); the CLI drives the same
core, so a Dagster backfill over a range the CLI already loaded shows green partitions and is a
proven no-op (matching content hashes).
"""
# NB: no ``from __future__ import annotations`` here -- Dagster resolves the ``context``
# parameter annotation at runtime, and stringized annotations break inside the factory.

from collections import Counter
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dagster import (
    AssetExecutionContext,
    AssetsDefinition,
    MaterializeResult,
    asset,
)

from energy_platform.connectors.open_meteo import WEATHER_VARIABLES
from energy_platform.connectors.types import Dataset, Resolution
from energy_platform.orchestration.ingest import ingest_forecast_vintage, ingest_partition
from energy_platform.orchestration.partitions import (
    PARTITION_TIMEZONE,
    daily_de_partitions,
    daily_forecast_partitions,
    daily_weather_partitions,
    partition_key_to_date,
)
from energy_platform.orchestration.resources import (
    OpenMeteoArchiveClientResource,
    OpenMeteoForecastClientResource,
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


# -- Weather (Open-Meteo) --------------------------------------------------------------


@asset(
    name="open_meteo_weather_actuals_raw",
    description=(
        "Open-Meteo historical weather actuals (irradiance, temperature, cloud cover, wind) "
        "for the site, hourly in UTC, one Europe/Berlin day per partition. Each variable is "
        "ingested as its own series, stored append-only in the raw zone alongside prices."
    ),
    partitions_def=daily_weather_partitions,
    group_name="weather",
    kinds={"python", "postgres"},
)
def open_meteo_weather_actuals_raw(
    context: AssetExecutionContext,
    open_meteo_archive: OpenMeteoArchiveClientResource,
    raw_zone: RawZonePostgresResource,
) -> MaterializeResult:
    day = partition_key_to_date(context.partition_key)
    site_id = open_meteo_archive.default_site_id

    outcomes: Counter[str] = Counter()
    row_total = 0
    null_total = 0
    # One HTTP call covers every variable (memoised in the client); we ingest each as its own
    # single-valued series so the raw zone and content hashing stay unchanged from M1.
    with raw_zone.get_repository() as repo, open_meteo_archive.get_client() as client:
        for variable in WEATHER_VARIABLES:
            result = ingest_partition(
                client,
                repo,
                variable,
                site_id,
                RESOLUTION,
                day,
                dagster_run_id=context.run_id,
            )
            outcomes[result.outcome.value] += 1
            row_total += result.row_count
            null_total += result.null_count

    context.log.info(
        "weather actuals %s @ %s: %d variables (%s), %d rows, %d null",
        day.isoformat(),
        site_id,
        len(WEATHER_VARIABLES),
        dict(outcomes),
        row_total,
        null_total,
    )
    return MaterializeResult(
        metadata={
            "site": site_id,
            "variable_count": len(WEATHER_VARIABLES),
            "loaded": outcomes.get("loaded", 0),
            "noop": outcomes.get("noop", 0),
            "revision": outcomes.get("revision", 0),
            "row_count": row_total,
            "null_count": null_total,
            "all_noop": row_total > 0 and outcomes.get("noop", 0) == len(WEATHER_VARIABLES),
        }
    )


@asset(
    name="open_meteo_weather_forecast_raw",
    description=(
        "Open-Meteo forecast horizon for the site, captured daily as an immutable vintage "
        "keyed by issue date. Append-only, never collapsed to 'latest' -- so backtests can "
        "reconstruct exactly what was forecast at decision time. Only the current issue date "
        "is materialisable; past forecast partitions cannot be backfilled."
    ),
    partitions_def=daily_forecast_partitions,
    group_name="weather",
    kinds={"python", "postgres"},
)
def open_meteo_weather_forecast_raw(
    context: AssetExecutionContext,
    open_meteo_forecast: OpenMeteoForecastClientResource,
    raw_zone: RawZonePostgresResource,
) -> MaterializeResult:
    issue_day = partition_key_to_date(context.partition_key)
    site_id = open_meteo_forecast.default_site_id
    issue_time = datetime.now(UTC)
    # Partition keys are Berlin calendar days, so compare against the Berlin-local date of the
    # issue instant -- not its UTC date, which straddles a different day around local midnight
    # and would flag a legitimately-today vintage as mislabelled.
    issue_local_day = issue_time.astimezone(ZoneInfo(PARTITION_TIMEZONE)).date()

    # The forecast API only serves the current issue: materialising an old issue date would
    # store today's forecast mislabelled. Warn rather than fabricate a past vintage.
    if issue_day != issue_local_day:
        context.log.warning(
            "forecast partition %s materialised on %s (Europe/Berlin): the API returns the "
            "CURRENT forecast, so this vintage reflects today's issue, not %s. Forecasts accrue "
            "forward only and cannot be backfilled.",
            issue_day.isoformat(),
            issue_local_day.isoformat(),
            issue_day.isoformat(),
        )

    with raw_zone.get_repository() as repo, open_meteo_forecast.get_client() as client:
        result = ingest_forecast_vintage(
            client,
            repo,
            site_id,
            RESOLUTION,
            issue_day,
            issue_time,
            dagster_run_id=context.run_id,
        )

    context.log.info(
        "weather forecast vintage %s @ %s: %s (%d rows across %d variables, %d null, %dd horizon)",
        issue_day.isoformat(),
        site_id,
        result.outcome.value,
        result.row_count,
        result.variable_count,
        result.null_count,
        result.horizon_days,
    )
    return MaterializeResult(
        metadata={
            "site": site_id,
            "issue_date": issue_day.isoformat(),
            "issue_time": issue_time.isoformat(),
            "outcome": result.outcome.value,
            "is_noop": result.is_noop,
            "content_hash": result.content_hash,
            "row_count": result.row_count,
            "variable_count": result.variable_count,
            "null_count": result.null_count,
            "horizon_days": result.horizon_days,
            "has_missing": result.has_missing,
        }
    )


weather_assets = [open_meteo_weather_actuals_raw, open_meteo_weather_forecast_raw]
