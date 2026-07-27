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

from energy_platform.config import AppConfig
from energy_platform.connectors.open_meteo import WEATHER_VARIABLES
from energy_platform.connectors.synthetic import TELEMETRY_DATASETS
from energy_platform.connectors.types import Dataset, Resolution
from energy_platform.dispatch.windows import load_coverage_windows
from energy_platform.orchestration.ingest import (
    ingest_forecast_vintage,
    ingest_partition,
    require_weather_ingested,
)
from energy_platform.orchestration.partitions import (
    PARTITION_TIMEZONE,
    daily_de_partitions,
    daily_forecast_partitions,
    daily_telemetry_partitions,
    daily_weather_partitions,
    partition_key_to_date,
)
from energy_platform.orchestration.resources import (
    ForecastPostgresResource,
    HomeAssistantResource,
    OpenMeteoArchiveClientResource,
    OpenMeteoForecastClientResource,
    RawZonePostgresResource,
    SmardClientResource,
    SyntheticTelemetryResource,
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


# -- Household telemetry (synthetic demo + real Fenecon) -------------------------------


def _telemetry_result(
    context: AssetExecutionContext,
    outcomes: "Counter[str]",
    row_total: int,
    null_total: int,
    site_id: str,
    label: str,
) -> MaterializeResult:
    n = len(TELEMETRY_DATASETS)
    context.log.info(
        "%s @ %s: %d series (%s), %d rows, %d null",
        label,
        site_id,
        n,
        dict(outcomes),
        row_total,
        null_total,
    )
    return MaterializeResult(
        metadata={
            "site": site_id,
            "series_count": n,
            "loaded": outcomes.get("loaded", 0),
            "noop": outcomes.get("noop", 0),
            "revision": outcomes.get("revision", 0),
            "row_count": row_total,
            "null_count": null_total,
            "all_noop": row_total > 0 and outcomes.get("noop", 0) == n,
        }
    )


@asset(
    name="synthetic_telemetry_raw",
    description=(
        "Synthetic household telemetry (PV, load, battery charge/discharge, SoC, grid "
        "import/export) for the site, hourly, one Europe/Berlin day per partition. A pure "
        "function of (config, date) and the day's ingested irradiance -- so demo mode is "
        "indistinguishable in shape from real telemetry and re-ingestion is a content-hash "
        "no-op. Depends on weather actuals: PV derives from that day's irradiance."
    ),
    partitions_def=daily_telemetry_partitions,
    deps=[open_meteo_weather_actuals_raw],
    group_name="telemetry",
    kinds={"python", "postgres"},
)
def synthetic_telemetry_raw(
    context: AssetExecutionContext,
    synthetic_telemetry: SyntheticTelemetryResource,
    raw_zone: RawZonePostgresResource,
) -> MaterializeResult:
    day = partition_key_to_date(context.partition_key)
    site_id = synthetic_telemetry.default_site_id

    outcomes: Counter[str] = Counter()
    row_total = 0
    null_total = 0
    # One repository serves as both the irradiance reader (the client pulls the day's weather
    # through it) and the write target, so the coupled simulation runs once and all seven series
    # persist over a single connection.
    with raw_zone.get_repository() as repo:
        # Same guard the CLI backfill uses: telemetry schedules run independently of the weather
        # schedule (and the asset dep does not order separately-scheduled jobs), so a missing/late
        # weather partition must fail the run loudly rather than emit all-null PV as green.
        require_weather_ingested(repo, site_id, [day])
        client = synthetic_telemetry.build(repo)
        for dataset in TELEMETRY_DATASETS:
            result = ingest_partition(
                client, repo, dataset, site_id, RESOLUTION, day, dagster_run_id=context.run_id
            )
            outcomes[result.outcome.value] += 1
            row_total += result.row_count
            null_total += result.null_count

    return _telemetry_result(
        context, outcomes, row_total, null_total, site_id, "synthetic telemetry"
    )


@asset(
    name="fenecon_telemetry_raw",
    description=(
        "Real Fenecon Home 10 telemetry via the read-only Home Assistant connector, hourly, one "
        "Europe/Berlin day per partition. Disabled by default and never scheduled: it runs only "
        "where ENERGY_HA_* credentials are present (verified manually over Tailscale). Lands in "
        "the identical schema as synthetic telemetry, differing only in source ('fenecon')."
    ),
    partitions_def=daily_telemetry_partitions,
    group_name="telemetry",
    kinds={"python", "postgres"},
)
def fenecon_telemetry_raw(
    context: AssetExecutionContext,
    home_assistant: HomeAssistantResource,
    raw_zone: RawZonePostgresResource,
) -> MaterializeResult:
    day = partition_key_to_date(context.partition_key)
    site_id = home_assistant.default_site_id

    outcomes: Counter[str] = Counter()
    row_total = 0
    null_total = 0
    with raw_zone.get_repository() as repo, home_assistant.get_client() as client:
        for dataset in TELEMETRY_DATASETS:
            result = ingest_partition(
                client, repo, dataset, site_id, RESOLUTION, day, dagster_run_id=context.run_id
            )
            outcomes[result.outcome.value] += 1
            row_total += result.row_count
            null_total += result.null_count

    return _telemetry_result(context, outcomes, row_total, null_total, site_id, "fenecon telemetry")


# The synthetic asset is the demo/CI path and is scheduled; the real Fenecon asset is defined so
# a credentialed deployment can materialise it, but stays out of the default schedules.
telemetry_assets = [synthetic_telemetry_raw, fenecon_telemetry_raw]


# -- Forecasting (M7) -------------------------------------------------------------------


@asset(
    name="forecast_backtest_derived",
    description=(
        "Backtest the PV and load forecast models over every declared coverage window and "
        "replace derived.forecast_runs / derived.forecast_predictions. Unpartitioned, because a "
        "backtest is a property of the whole declared coverage rather than of a day. Reads "
        "mart_hourly_energy and stg_weather_forecast, so the warehouse must be built first -- the "
        "dbt -> python -> dbt ordering lives in `just warehouse` and in the CI dbt job, not here: "
        "Dagster cannot express a dependency on a dbt model this code location does not own."
    ),
    group_name="forecasting",
    kinds={"python", "postgres", "sklearn"},
)
def forecast_backtest_derived(
    context: AssetExecutionContext,
    forecast_store: ForecastPostgresResource,
) -> MaterializeResult:
    # Imported inside the asset for the same reason the CLI does it: the scientific stack costs
    # real import time and the code location loads on every Dagster process start.
    from energy_platform.forecasting import runner
    from energy_platform.forecasting.store import run_payload
    from energy_platform.forecasting.vintage import PERSISTENCE_RULE_ID, SELECTION_RULE_ID

    config = AppConfig.from_env()
    site = config.site.default
    windows = load_coverage_windows()

    runs = predictions = skipped = 0
    with forecast_store.get_repository() as repo:
        repo.ensure_schema()  # type: ignore[attr-defined]
        has_observations, has_vintages = repo.input_relations_exist()  # type: ignore[attr-defined]
        if not (has_observations and has_vintages):
            raise RuntimeError(
                "forecast_backtest_derived needs mart_hourly_energy and stg_weather_forecast; "
                "run `just dbt-build` (or `just warehouse` for the full sequence) first"
            )
        for window in windows:
            observations = repo.read_observations(window.start, window.end, site.id)  # type: ignore[attr-defined]
            vintages = repo.read_vintages(  # type: ignore[attr-defined]
                site.id, config.forecast.forecast_source, window.start, window.end
            )
            if not observations:
                continue
            for target in runner.TARGETS:
                result = runner.backtest(
                    site,
                    target,
                    window.start,
                    window.end,
                    observations,
                    vintages,
                    config=config.forecast,
                    pv=config.pv,
                )
                skipped += len(result.skipped_days)
                runs += len(result.runs)
                if result.runs:
                    counts = repo.replace_window(  # type: ignore[attr-defined]
                        [
                            run_payload(
                                run, config.forecast, SELECTION_RULE_ID, PERSISTENCE_RULE_ID
                            )
                            for run in result.runs
                        ]
                    )
                    predictions += counts.predictions

    context.log.info("Backtest complete: runs=%d predictions=%d", runs, predictions)
    return MaterializeResult(
        metadata={
            "runs": runs,
            "predictions": predictions,
            "days_without_a_vintage": skipped,
            "windows": len(windows),
            "vintage_source": config.forecast.forecast_source,
            "observation_lag_hours": config.forecast.telemetry_lag_hours,
        }
    )


forecasting_assets = [forecast_backtest_derived]
