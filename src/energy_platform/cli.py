"""Command-line entrypoint for the energy platform.

``energy-platform backfill`` loads SMARD history into the raw zone by driving the same
idempotent :func:`ingest_partition` core the Dagster assets use -- directly against
Postgres, so it needs no running Dagster instance. It is idempotent (re-running is a
content-hash no-op) and resumable (each partition commits independently, so an interrupted
run simply continues on retry).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta

import httpx
import psycopg

from energy_platform.config import AppConfig
from energy_platform.connectors.smard import USER_AGENT, SmardClient
from energy_platform.connectors.types import Dataset, Resolution
from energy_platform.orchestration.ingest import IngestResult, ingest_partition
from energy_platform.orchestration.partition_config import PARTITION_TIMEZONE
from energy_platform.orchestration.raw_zone import RawZoneRepository, WriteOutcome

logger = logging.getLogger("energy_platform.backfill")

# CLI dataset aliases -> connector datasets.
_DATASET_ALIASES: dict[str, Dataset] = {
    "price": Dataset.DAY_AHEAD_PRICE,
    "load": Dataset.GRID_LOAD,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "backfill":
        return _run_backfill(args)
    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="energy-platform")
    sub = parser.add_subparsers(dest="command")

    backfill = sub.add_parser(
        "backfill",
        help="Load SMARD history into the raw zone (idempotent, resumable).",
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
        help="Comma-separated datasets: price, load (default: both).",
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
        help="SMARD region code (default: DE).",
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

    with (
        httpx.Client(
            timeout=config.smard.timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        ) as http,
        psycopg.connect(config.postgres.dsn) as conn,
    ):
        client = SmardClient(
            http, base_url=config.smard.base_url, max_retries=config.smard.max_retries
        )
        repo = RawZoneRepository(conn, schema=config.postgres.schema)
        repo.ensure_schema()

        for processed, day in enumerate(days, start=1):
            for dataset in datasets:
                try:
                    result = ingest_partition(client, repo, dataset, args.region, resolution, day)
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


def _parse_datasets(raw: str) -> list[Dataset]:
    datasets: list[Dataset] = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        if token not in _DATASET_ALIASES:
            valid = ", ".join(_DATASET_ALIASES)
            raise SystemExit(f"unknown dataset '{token}'; choose from: {valid}")
        datasets.append(_DATASET_ALIASES[token])
    if not datasets:
        raise SystemExit("no datasets selected")
    return datasets


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
