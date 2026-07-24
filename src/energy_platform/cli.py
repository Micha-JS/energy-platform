"""Command-line entrypoint for the energy platform.

``energy-platform backfill`` loads settled history (SMARD market data and Open-Meteo weather
actuals) into the raw zone by driving the same idempotent :func:`ingest_partition` core the
Dagster assets use -- directly against Postgres, so it needs no running Dagster instance. It is
idempotent (re-running is a content-hash no-op) and resumable (each partition commits
independently, so an interrupted run simply continues on retry).

``energy-platform forecast-snapshot`` captures *today's* Open-Meteo forecast as one immutable
vintage. Forecasts are never backfillable -- the API only serves the current issue -- so this
command has no date range; the daily Dagster schedule is the primary accrual path.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import psycopg

from energy_platform.config import AppConfig
from energy_platform.connectors.base import MarketDataConnector
from energy_platform.connectors.open_meteo import USER_AGENT as OPEN_METEO_USER_AGENT
from energy_platform.connectors.open_meteo import (
    WEATHER_VARIABLES,
    OpenMeteoArchiveClient,
    OpenMeteoForecastClient,
)
from energy_platform.connectors.smard import USER_AGENT, SmardClient
from energy_platform.connectors.types import Dataset, Resolution
from energy_platform.orchestration.ingest import (
    IngestResult,
    ingest_forecast_vintage,
    ingest_partition,
)
from energy_platform.orchestration.partition_config import PARTITION_TIMEZONE
from energy_platform.orchestration.raw_zone import RawZoneRepository, WriteOutcome

logger = logging.getLogger("energy_platform.backfill")

# CLI market-data aliases -> connector datasets. Weather is requested via the "weather" alias,
# which expands to every weather variable (they share one API call and one raw table).
_MARKET_ALIASES: dict[str, Dataset] = {
    "price": Dataset.DAY_AHEAD_PRICE,
    "load": Dataset.GRID_LOAD,
}
_WEATHER_ALIAS = "weather"
_WEATHER_DATASETS = frozenset(WEATHER_VARIABLES)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "backfill":
        return _run_backfill(args)
    if args.command == "forecast-snapshot":
        return _run_forecast_snapshot(args)
    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="energy-platform")
    sub = parser.add_subparsers(dest="command")

    backfill = sub.add_parser(
        "backfill",
        help="Load settled history (market data + weather actuals) into the raw zone.",
    )
    backfill.add_argument(
        "--from",
        dest="from_date",
        required=True,
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="First Europe/Berlin day to load (inclusive).",
    )
    backfill.add_argument(
        "--to",
        dest="to_date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Last day to load (inclusive). Defaults to yesterday (Europe/Berlin).",
    )
    backfill.add_argument(
        "--datasets",
        default="price,load",
        help="Comma-separated: price, load, weather (default: price,load). "
        "'weather' expands to all Open-Meteo weather variables.",
    )
    backfill.add_argument(
        "--resolution",
        default=Resolution.HOUR.value,
        choices=[r.value for r in Resolution],
        help="Temporal resolution (default: hour).",
    )
    backfill.add_argument(
        "--region",
        default="DE",
        help="SMARD region code for market datasets (default: DE).",
    )
    backfill.add_argument(
        "--site",
        default=None,
        help="Site id for weather datasets (default: the configured default site).",
    )

    snapshot = sub.add_parser(
        "forecast-snapshot",
        help="Capture today's Open-Meteo forecast as an immutable vintage (not backfillable).",
    )
    snapshot.add_argument(
        "--site",
        default=None,
        help="Site id to snapshot (default: the configured default site).",
    )
    return parser


def _run_backfill(args: argparse.Namespace) -> int:
    datasets = _parse_datasets(args.datasets)
    resolution = Resolution(args.resolution)
    to_date = args.to_date or _yesterday_berlin()
    if to_date < args.from_date:
        logger.error("--to %s is before --from %s", to_date, args.from_date)
        return 2

    days = list(_date_range(args.from_date, to_date))
    logger.info(
        "Backfilling %d day(s) %s..%s for %s at %s resolution",
        len(days),
        args.from_date,
        to_date,
        ", ".join(d.value for d in datasets),
        resolution.value,
    )

    config = AppConfig.from_env()
    outcomes: Counter[WriteOutcome] = Counter()
    days_with_missing = 0
    failures = 0

    with psycopg.connect(config.postgres.dsn) as conn, ExitStack() as stack:
        repo = RawZoneRepository(conn, schema=config.postgres.schema)
        repo.ensure_schema()
        routes = _build_routes(datasets, config, args, stack)

        for processed, day in enumerate(days, start=1):
            for dataset in datasets:
                client, region = routes[dataset]
                # Weather actuals are always hourly regardless of --resolution (which only
                # governs SMARD's quarter-hour series); forcing it keeps the stored resolution
                # label and expected_count honest instead of mislabelling weather as quarterhour.
                dataset_resolution = (
                    Resolution.HOUR if dataset in _WEATHER_DATASETS else resolution
                )
                try:
                    result = ingest_partition(
                        client, repo, dataset, region, dataset_resolution, day
                    )
                except Exception:
                    failures += 1
                    logger.exception("Failed to ingest %s %s", dataset.value, day)
                    continue
                outcomes[result.outcome] += 1
                if result.has_missing:
                    days_with_missing += 1
                    _log_missing(dataset, result)
            if processed % 90 == 0 or processed == len(days):
                logger.info("  progress: %d/%d days", processed, len(days))

    _log_summary(outcomes, days_with_missing, failures)
    return 1 if failures else 0


def _build_routes(
    datasets: list[Dataset],
    config: AppConfig,
    args: argparse.Namespace,
    stack: ExitStack,
) -> dict[Dataset, tuple[MarketDataConnector, str]]:
    """Map each requested dataset to its connector and region, building clients as needed.

    Market datasets route to SMARD (region = ``--region``); weather variables route to the
    Open-Meteo archive (region = the site id). Both connectors satisfy the same protocol, so
    the ``ingest_partition`` loop is connector-agnostic.
    """
    routes: dict[Dataset, tuple[MarketDataConnector, str]] = {}
    market = [d for d in datasets if d not in _WEATHER_DATASETS]
    weather = [d for d in datasets if d in _WEATHER_DATASETS]

    if market:
        http = stack.enter_context(
            httpx.Client(
                timeout=config.smard.timeout_seconds,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        )
        smard = SmardClient(
            http, base_url=config.smard.base_url, max_retries=config.smard.max_retries
        )
        for dataset in market:
            routes[dataset] = (smard, args.region)

    if weather:
        http = stack.enter_context(
            httpx.Client(
                timeout=config.open_meteo.timeout_seconds,
                headers={"User-Agent": OPEN_METEO_USER_AGENT, "Accept": "application/json"},
            )
        )
        archive = OpenMeteoArchiveClient(
            http,
            config.site.coordinates,
            base_url=config.open_meteo.archive_url,
            max_retries=config.open_meteo.max_retries,
        )
        site_id = args.site or config.site.default_id
        for dataset in weather:
            routes[dataset] = (archive, site_id)

    return routes


def _run_forecast_snapshot(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    site_id = args.site or config.site.default_id
    coordinates = config.site.coordinates
    # The as-of instant is now; the issue date is today in the partition calendar's timezone.
    issue_time = datetime.now(UTC)
    issue_day = datetime.now(ZoneInfo(PARTITION_TIMEZONE)).date()

    logger.info("Capturing forecast vintage for site %s (issue date %s)", site_id, issue_day)
    with (
        httpx.Client(
            timeout=config.open_meteo.timeout_seconds,
            headers={"User-Agent": OPEN_METEO_USER_AGENT, "Accept": "application/json"},
        ) as http,
        psycopg.connect(config.postgres.dsn) as conn,
    ):
        client = OpenMeteoForecastClient(
            http,
            coordinates,
            base_url=config.open_meteo.forecast_url,
            forecast_days=config.open_meteo.forecast_days,
            max_retries=config.open_meteo.max_retries,
        )
        repo = RawZoneRepository(conn, schema=config.postgres.schema)
        repo.ensure_schema()
        try:
            result = ingest_forecast_vintage(
                client, repo, site_id, Resolution.HOUR, issue_day, issue_time
            )
        except Exception:
            logger.exception("Failed to capture forecast vintage for site %s", site_id)
            return 1

    logger.info(
        "Done. vintage %s: %s (%d rows across %d variables, %d null, %dd horizon)",
        result.issue_date,
        result.outcome.value,
        result.row_count,
        result.variable_count,
        result.null_count,
        result.horizon_days,
    )
    return 0


def _parse_datasets(raw: str) -> list[Dataset]:
    datasets: list[Dataset] = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        if token in _MARKET_ALIASES:
            datasets.append(_MARKET_ALIASES[token])
        elif token == _WEATHER_ALIAS:
            datasets.extend(WEATHER_VARIABLES)
        else:
            valid = ", ".join([*_MARKET_ALIASES, _WEATHER_ALIAS])
            raise SystemExit(f"unknown dataset '{token}'; choose from: {valid}")
    if not datasets:
        raise SystemExit("no datasets selected")
    # Dedupe, preserving first-seen order (so 'weather,price' stays stable and unique).
    seen: set[Dataset] = set()
    unique: list[Dataset] = []
    for dataset in datasets:
        if dataset not in seen:
            seen.add(dataset)
            unique.append(dataset)
    return unique


def _date_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _yesterday_berlin() -> date:
    from zoneinfo import ZoneInfo

    return (datetime.now(ZoneInfo(PARTITION_TIMEZONE)) - timedelta(days=1)).date()


def _log_missing(dataset: Dataset, result: IngestResult) -> None:
    logger.warning(
        "  %s %s: %d/%d intervals present, %d null (missing left as-is)",
        dataset.value,
        result.partition_date,
        result.row_count,
        result.expected_count,
        result.null_count,
    )


def _log_summary(outcomes: Counter[WriteOutcome], days_with_missing: int, failures: int) -> None:
    logger.info(
        "Done. loaded=%d noop=%d revision=%d | partitions_with_missing=%d failures=%d",
        outcomes[WriteOutcome.LOADED],
        outcomes[WriteOutcome.NOOP],
        outcomes[WriteOutcome.REVISION],
        days_with_missing,
        failures,
    )


if __name__ == "__main__":
    sys.exit(main())
