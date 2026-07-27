"""Command-line entrypoint for the energy platform.

``energy-platform backfill`` loads settled history (SMARD market data and Open-Meteo weather
actuals) into the raw zone by driving the same idempotent :func:`ingest_partition` core the
Dagster assets use -- directly against Postgres, so it needs no running Dagster instance. It is
idempotent (re-running is a content-hash no-op) and resumable (each partition commits
independently, so an interrupted run simply continues on retry).

``energy-platform forecast-snapshot`` captures *today's* Open-Meteo forecast as one immutable
vintage. Forecasts are never backfillable -- the API only serves the current issue -- so this
command has no date range; the daily Dagster schedule is the primary accrual path.

``energy-platform dispatch`` solves the M6 battery optimiser over each declared coverage window and
writes the four scenarios to the derived zone. Unlike the two above it reads a *mart* rather than
the raw zone, so it runs after ``dbt build`` and before the dispatch mart is built -- see the
justfile and the CI dbt job for the ordering.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import psycopg

from energy_platform.config import AppConfig
from energy_platform.connectors.base import MarketDataConnector
from energy_platform.connectors.offline import (
    DEFAULT_FIXTURES_DIR,
    forecast_fixture_issue_date,
    offline_transport,
)
from energy_platform.connectors.open_meteo import USER_AGENT as OPEN_METEO_USER_AGENT
from energy_platform.connectors.open_meteo import (
    WEATHER_VARIABLES,
    OpenMeteoArchiveClient,
    OpenMeteoForecastClient,
)
from energy_platform.connectors.smard import USER_AGENT, SmardClient
from energy_platform.connectors.synthetic import (
    TELEMETRY_DATASETS,
    SyntheticTelemetryClient,
)
from energy_platform.connectors.types import Dataset, Resolution
from energy_platform.dispatch import runner
from energy_platform.dispatch.optimizer import DispatchError, solver_version
from energy_platform.dispatch.store import DispatchInputError, DispatchRepository
from energy_platform.dispatch.windows import CoverageWindow, load_coverage_windows
from energy_platform.orchestration.ingest import (
    IngestResult,
    WeatherDependencyError,
    ingest_forecast_vintage,
    ingest_partition,
    require_weather_ingested,
)
from energy_platform.orchestration.partition_config import PARTITION_TIMEZONE
from energy_platform.orchestration.raw_zone import RawZoneRepository, WriteOutcome
from energy_platform.tariffs.catalog import TariffSpec, load_catalog, resolve

logger = logging.getLogger("energy_platform.backfill")

# CLI market-data aliases -> connector datasets. Weather is requested via the "weather" alias,
# which expands to every weather variable (they share one API call and one raw table).
_MARKET_ALIASES: dict[str, Dataset] = {
    "price": Dataset.DAY_AHEAD_PRICE,
    "load": Dataset.GRID_LOAD,
}
_WEATHER_ALIAS = "weather"
_WEATHER_DATASETS = frozenset(WEATHER_VARIABLES)
_TELEMETRY_ALIAS = "telemetry"
_TELEMETRY_DATASETS = frozenset(TELEMETRY_DATASETS)
# Telemetry and weather are hourly regardless of --resolution (which only governs SMARD's
# quarter-hour series); the site irradiance PV derives from is hourly.
_HOURLY_ONLY = _WEATHER_DATASETS | _TELEMETRY_DATASETS

# Ingestion ordering within a day: a dataset's group must run after any group it depends on.
# Telemetry derives PV from ingested weather, so weather precedes telemetry; market data is
# independent. Lower rank runs first.
_GROUP_RANK: dict[str, int] = {"market": 0, "weather": 1, "telemetry": 2}

# Spellings that enable offline mode via the ENERGY_OFFLINE env var (the --offline flag is the
# primary switch). Kept deliberately small; anything else is treated as "not set".
_OFFLINE_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _resolve_offline(args: argparse.Namespace) -> Path | None:
    """Return the fixtures directory when offline mode is active, else ``None``.

    Offline mode is enabled by ``--offline`` or ``ENERGY_OFFLINE`` (a secondary env switch). When
    active it logs an unmissable line so a real run can never be silently served from fixtures.
    """
    env = os.environ.get("ENERGY_OFFLINE", "").strip().lower() in _OFFLINE_TRUTHY
    if not (getattr(args, "offline", False) or env):
        return None
    fixtures_dir = getattr(args, "fixtures_dir", None) or DEFAULT_FIXTURES_DIR
    logger.warning(
        "OFFLINE MODE -- serving recorded fixtures from %s (no live API calls)", fixtures_dir
    )
    return fixtures_dir


def _http_client(offline_dir: Path | None, *, timeout: float, user_agent: str) -> httpx.Client:
    """Build an HTTP client -- fixture-backed when offline, otherwise a real networked client."""
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if offline_dir is not None:
        return httpx.Client(transport=offline_transport(offline_dir), headers=headers)
    return httpx.Client(timeout=timeout, headers=headers)


def _berlin_midnight(day: date) -> datetime:
    """The UTC instant of Berlin midnight starting ``day`` -- a fixed, deterministic as-of time."""
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(PARTITION_TIMEZONE)).astimezone(UTC)


def _dataset_group(dataset: Dataset) -> str:
    if dataset in _TELEMETRY_DATASETS:
        return "telemetry"
    if dataset in _WEATHER_DATASETS:
        return "weather"
    return "market"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "backfill":
        return _run_backfill(args)
    if args.command == "forecast-snapshot":
        return _run_forecast_snapshot(args)
    if args.command == "dispatch":
        return _run_dispatch(args)
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
        help="Comma-separated: price, load, weather, telemetry (default: price,load). "
        "'weather' expands to all Open-Meteo weather variables; 'telemetry' expands to the "
        "synthetic household series and requires that range's weather (run 'weather,telemetry' "
        "to seed both in dependency order).",
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
    _add_offline_args(backfill)

    snapshot = sub.add_parser(
        "forecast-snapshot",
        help="Capture today's Open-Meteo forecast as an immutable vintage (not backfillable).",
    )
    snapshot.add_argument(
        "--site",
        default=None,
        help="Site id to snapshot (default: the configured default site).",
    )
    snapshot.add_argument(
        "--issue-date",
        dest="issue_date",
        default=None,
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Pin the vintage's issue date (issue_time = Berlin midnight of that day) instead of "
        "'now'. Rejected if the fetched horizon reaches past it. Default: today -- or, with "
        "--offline, the issue date the recorded fixture actually represents.",
    )
    _add_offline_args(snapshot)

    dispatch = sub.add_parser(
        "dispatch",
        help="Solve the battery dispatch optimiser over the declared coverage windows.",
    )
    dispatch.add_argument(
        "--from",
        dest="from_date",
        default=None,
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Solve this ad-hoc window instead of the declared ones (requires --to). By default "
        "every window in dbt/dbt_project.yml's coverage_windows var is solved -- the same "
        "declaration the hourly spine is built from, read from the same file so the two cannot "
        "drift.",
    )
    dispatch.add_argument(
        "--to",
        dest="to_date",
        default=None,
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Last Europe/Berlin day of the ad-hoc window (inclusive). Requires --from.",
    )
    dispatch.add_argument(
        "--site",
        default=None,
        help="Site id to solve for (default: every site the energy mart holds in the window).",
    )
    dispatch.add_argument(
        "--tariff",
        dest="tariffs",
        default=None,
        help="Comma-separated consumption tariff ids (default: every static/dynamic row in the "
        "catalogue).",
    )
    return parser


def _add_offline_args(subparser: argparse.ArgumentParser) -> None:
    """Attach the shared ``--offline`` / ``--fixtures-dir`` switches to a subcommand."""
    subparser.add_argument(
        "--offline",
        action="store_true",
        help="Serve SMARD/Open-Meteo from recorded fixtures instead of the live APIs "
        "(deterministic, no network). Also enabled by ENERGY_OFFLINE=1.",
    )
    subparser.add_argument(
        "--fixtures-dir",
        dest="fixtures_dir",
        default=None,
        type=Path,
        help="Directory of recorded fixtures for --offline (default: the repo's test fixtures).",
    )


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
    offline_dir = _resolve_offline(args)
    outcomes: Counter[WriteOutcome] = Counter()
    days_with_missing = 0
    failures = 0

    with psycopg.connect(config.postgres.dsn) as conn, ExitStack() as stack:
        repo = RawZoneRepository(conn, schema=config.postgres.schema)
        repo.ensure_schema()
        site_id = args.site or config.site.default_id
        _require_weather_dependency(repo, datasets, days, site_id)
        routes = _build_routes(datasets, config, args, stack, repo, site_id, offline_dir)

        for processed, day in enumerate(days, start=1):
            for dataset in datasets:
                client, region = routes[dataset]
                # Weather and telemetry are always hourly regardless of --resolution (which only
                # governs SMARD's quarter-hour series); forcing it keeps the stored resolution
                # label and expected_count honest instead of mislabelling them as quarterhour.
                dataset_resolution = Resolution.HOUR if dataset in _HOURLY_ONLY else resolution
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
    repo: RawZoneRepository,
    site_id: str,
    offline_dir: Path | None,
) -> dict[Dataset, tuple[MarketDataConnector, str]]:
    """Map each requested dataset to its connector and region, building clients as needed.

    Market datasets route to SMARD (region = ``--region``); weather and telemetry route to
    ``site_id`` (resolved once by the caller). Telemetry is generated by the synthetic client,
    which reads the day's ingested irradiance back through ``repo``. Every connector satisfies the
    same protocol, so the ``ingest_partition`` loop is connector-agnostic. When ``offline_dir`` is
    set, the HTTP clients are backed by recorded fixtures instead of the live APIs.
    """
    routes: dict[Dataset, tuple[MarketDataConnector, str]] = {}
    market = [d for d in datasets if _dataset_group(d) == "market"]
    weather = [d for d in datasets if d in _WEATHER_DATASETS]
    telemetry = [d for d in datasets if d in _TELEMETRY_DATASETS]

    if market:
        http = stack.enter_context(
            _http_client(offline_dir, timeout=config.smard.timeout_seconds, user_agent=USER_AGENT)
        )
        smard = SmardClient(
            http, base_url=config.smard.base_url, max_retries=config.smard.max_retries
        )
        for dataset in market:
            routes[dataset] = (smard, args.region)

    if weather:
        http = stack.enter_context(
            _http_client(
                offline_dir,
                timeout=config.open_meteo.timeout_seconds,
                user_agent=OPEN_METEO_USER_AGENT,
            )
        )
        archive = OpenMeteoArchiveClient(
            http,
            config.site.coordinates,
            base_url=config.open_meteo.archive_url,
            max_retries=config.open_meteo.max_retries,
        )
        for dataset in weather:
            routes[dataset] = (archive, site_id)

    if telemetry:
        # The synthetic generator reads irradiance back through the same repository the loop
        # writes telemetry to -- one connection serves both. Only synthetic telemetry is
        # backfillable; the real Fenecon connector is manual and never runs here.
        synthetic = SyntheticTelemetryClient(
            repo,
            pv=config.pv,
            battery=config.battery,
            synthetic=config.synthetic,
        )
        for dataset in telemetry:
            routes[dataset] = (synthetic, site_id)

    return routes


def _require_weather_dependency(
    repo: RawZoneRepository,
    datasets: list[Dataset],
    days: list[date],
    site_id: str,
) -> None:
    """Fail loudly if telemetry is requested for days whose weather has not been ingested.

    When ``weather`` is requested in the same run it ingests first (datasets are group-ordered),
    so the dependency is satisfied in-flight and no check is needed. Otherwise telemetry relies
    on previously-loaded irradiance; a missing day would silently yield all-null PV, so we stop
    with an actionable message instead.
    """
    wants_telemetry = any(d in _TELEMETRY_DATASETS for d in datasets)
    weather_in_run = any(d in _WEATHER_DATASETS for d in datasets)
    if not wants_telemetry or weather_in_run:
        return

    try:
        require_weather_ingested(
            repo,
            site_id,
            days,
            hint="Run with --datasets weather,telemetry to seed both in dependency order, or "
            "backfill weather for this range first.",
        )
    except WeatherDependencyError as exc:
        raise SystemExit(str(exc)) from exc


def _run_forecast_snapshot(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    offline_dir = _resolve_offline(args)
    site_id = args.site or config.site.default_id
    coordinates = config.site.coordinates
    if args.issue_date is not None:
        # Pinned, deterministic vintage: the as-of instant is Berlin midnight of the issue day
        # (a fixed UTC instant), so seeding the same day twice is a verifiable no-op.
        issue_day = args.issue_date
        issue_time = _berlin_midnight(issue_day)
    elif offline_dir is not None:
        # Offline serves one recorded snapshot, so "now" would label it with an issue date it does
        # not describe. Derive the vintage's real issue day from the fixture instead --
        # deterministic, without pinning a literal that drifts when the fixture is re-recorded.
        issue_day = forecast_fixture_issue_date(offline_dir)
        issue_time = _berlin_midnight(issue_day)
        logger.info("Offline vintage: issue date %s, derived from the recorded fixture", issue_day)
    else:
        # The as-of instant is now; the issue date is today in the partition calendar's timezone.
        issue_time = datetime.now(UTC)
        issue_day = datetime.now(ZoneInfo(PARTITION_TIMEZONE)).date()

    logger.info("Capturing forecast vintage for site %s (issue date %s)", site_id, issue_day)
    with (
        _http_client(
            offline_dir,
            timeout=config.open_meteo.timeout_seconds,
            user_agent=OPEN_METEO_USER_AGENT,
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


def _run_dispatch(args: argparse.Namespace) -> int:
    """Solve every (window, site, tariff) and replace its rows in the derived zone.

    Reads ``mart_hourly_energy``, so the warehouse must be built first -- a missing mart fails
    loudly with the command to run rather than writing an empty result. Each (window, site, tariff)
    is committed on its own, so an interrupted run leaves completed windows persisted and a re-run
    simply redoes the rest.
    """
    config = AppConfig.from_env()
    try:
        windows = _dispatch_windows(args)
        specs, feed_in = _dispatch_tariffs(args, config)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from exc

    logger.info(
        "Optimising %d window(s) %s for %d tariff(s) %s against battery %.1f kWh / %.1f kW / "
        "%.0f%% round trip",
        len(windows),
        ", ".join(str(w) for w in windows),
        len(specs),
        ", ".join(spec.tariff_id for spec in specs),
        config.battery.capacity_kwh,
        config.battery.max_charge_kw,
        config.battery.round_trip_efficiency * 100,
    )

    solved = 0
    failures = 0
    version = solver_version()

    with psycopg.connect(config.postgres.dsn) as conn:
        repo = DispatchRepository(
            conn,
            derived_schema=config.postgres.derived_schema,
            marts_schema=config.postgres.marts_schema,
        )
        repo.ensure_schema()
        if not repo.input_relation_exists():
            raise SystemExit(
                f"{config.postgres.marts_schema}.mart_hourly_energy does not exist; run "
                "`just dbt-build` before `just dispatch`"
            )

        for window in windows:
            regions = (args.site,) if args.site else repo.regions(window)
            if not regions:
                logger.warning(
                    "  %s: no site has telemetry in this window, nothing to solve", window
                )
                continue
            for region in regions:
                for spec in specs:
                    try:
                        hours = repo.read_window(window, region)
                        solution = runner.solve(
                            window, region, hours, spec, feed_in, config.battery
                        )
                        counts = repo.replace_window(solution, config.battery, version)
                    except (DispatchError, DispatchInputError):
                        failures += 1
                        logger.exception(
                            "Failed to solve %s %s under %s", window, region, spec.tariff_id
                        )
                        continue
                    solved += 1
                    _log_solution(window, region, solution, counts.replaced > 0)

    logger.info("Done. solved=%d failures=%d solver=highs/%s", solved, failures, version)
    return 1 if failures else 0


def _log_solution(
    window: CoverageWindow,
    region: str,
    solution: runner.WindowSolution,
    replaced: bool,
) -> None:
    """One line per solved (window, site, tariff): every scenario's objective, and the saving."""
    objectives = {result.scenario.value: result.objective_eur for result in solution.results}
    baseline = objectives["naive_continuous"]
    logger.info(
        "  %s %s %s: optimal %+.2f EUR vs naive %+.2f (saves %+.2f) | no-battery %+.2f | "
        "terminal %.2f ct/kWh%s",
        window,
        region,
        solution.tariff_id,
        objectives["optimal"],
        baseline,
        baseline - objectives["optimal"],
        objectives["no_battery"],
        solution.terminal_value_eur_kwh * 100,
        " [replaced]" if replaced else "",
    )


def _dispatch_windows(args: argparse.Namespace) -> tuple[CoverageWindow, ...]:
    """The windows to solve: an explicit ad-hoc range, or every one the dbt project declares."""
    if (args.from_date is None) != (args.to_date is None):
        raise ValueError("--from and --to must be given together, or neither")
    if args.from_date is not None and args.to_date is not None:
        return (CoverageWindow(start=args.from_date, end=args.to_date),)
    return load_coverage_windows()


def _dispatch_tariffs(
    args: argparse.Namespace, config: AppConfig
) -> tuple[tuple[TariffSpec, ...], TariffSpec]:
    """The consumption tariffs to price under, and the feed-in scheme in force.

    Defaults to every consumption row in the catalogue -- the marts compare tariffs, so solving
    only one would leave the comparison half-built. The feed-in scheme is chosen by config, mirror-
    ing the dbt ``feed_in_tariff_id`` var; exactly one applies at a time.
    """
    catalog = load_catalog()
    feed_in = resolve(config.tariffs.feed_in_tariff_id, catalog)
    if args.tariffs:
        ids = [token.strip() for token in args.tariffs.split(",") if token.strip()]
        if not ids:
            raise ValueError("--tariff was given but selected no tariffs")
        specs = tuple(resolve(tariff_id, catalog) for tariff_id in ids)
        for spec in specs:
            if not spec.is_consumption:
                raise ValueError(
                    f"tariff {spec.tariff_id!r} is a {spec.kind.value} row and prices exports, "
                    "not consumption"
                )
        return specs, feed_in
    return tuple(spec for spec in catalog.values() if spec.is_consumption), feed_in


def _parse_datasets(raw: str) -> list[Dataset]:
    datasets: list[Dataset] = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        if token in _MARKET_ALIASES:
            datasets.append(_MARKET_ALIASES[token])
        elif token == _WEATHER_ALIAS:
            datasets.extend(WEATHER_VARIABLES)
        elif token == _TELEMETRY_ALIAS:
            datasets.extend(TELEMETRY_DATASETS)
        else:
            valid = ", ".join([*_MARKET_ALIASES, _WEATHER_ALIAS, _TELEMETRY_ALIAS])
            raise SystemExit(f"unknown dataset '{token}'; choose from: {valid}")
    if not datasets:
        raise SystemExit("no datasets selected")
    # Dedupe (preserving first sight), then order by group so a dataset's dependencies ingest
    # first within each day -- weather before telemetry. Sorting is stable, so intra-group order
    # is preserved and market data stays where it was.
    seen: set[Dataset] = set()
    unique: list[Dataset] = []
    for dataset in datasets:
        if dataset not in seen:
            seen.add(dataset)
            unique.append(dataset)
    unique.sort(key=lambda d: _GROUP_RANK[_dataset_group(d)])
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
