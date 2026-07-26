"""Offline fixture transport for the ingestion connectors.

Serves recorded SMARD and Open-Meteo responses from a fixtures directory through a single
``httpx.MockTransport``, so the real connectors -- and therefore the shipped ``backfill`` /
``forecast-snapshot`` CLI -- can run with **zero network access**. This started life as test
scaffolding in ``tests/conftest.py``; it is promoted here to supported product code because two
things now depend on it beyond the unit tests:

* CI rebuilds the warehouse from the *literal* CLI (``energy-platform backfill --offline``), so a
  hermetic, deterministic seed path is a product feature, not a test shim.
* ``just demo`` can seed the whole stack offline, so a stranger cloning the repo sees the full
  flow with no SMARD/Open-Meteo availability risk.

Routing is by URL: Open-Meteo archive/forecast endpoints are matched by path suffix; everything
else is treated as a SMARD chart-data request. A request with no recorded fixture returns ``404``
with the expected filename, so a coverage gap fails loudly instead of silently serving stale data.

The fixture-name rules here are the single source of truth: ``tests/conftest.py`` and the
``scripts/record_*_fixtures.py`` recorders both map URLs to these same filenames.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

# ``.../src/energy_platform/connectors/offline.py`` -> repo root is three parents up from the
# package root; fixtures live under ``tests/connectors/fixtures``. The fixtures are test data, not
# package data, so they are not shipped in the wheel and cannot be located via
# ``importlib.resources``: outside a source checkout this path is meaningless, which is why
# ``offline_transport`` validates the directory instead of trusting it.
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "connectors" / "fixtures"

# The single recorded forecast snapshot. One recorded fixture is one honest vintage: the API only
# ever serves the current issue, so forecasts accrue forward and past vintages cannot be recorded.
FORECAST_FIXTURE = "open_meteo_forecast.json"

# ``OpenMeteoForecastClient``'s default: the vintage reaches one day *before* its issue day, so the
# issue day is the fixture's earliest target plus this many days. Kept as a constant rather than an
# import to keep this module independent of the client.
FORECAST_PAST_DAYS = 1


def smard_fixture_name(url: httpx.URL) -> str | None:
    """Map a SMARD request URL to its recorded fixture filename, or ``None`` if unroutable.

    ``.../{filter}/{region}/index_{res}.json`` -> ``{filter}_{region}_index_{res}.json``;
    weekly files already embed ``{filter}_{region}_{res}_{ts}`` and are used as-is. A path with
    fewer than three segments cannot be a chart-data request, so it routes to the same ``404`` an
    unrecorded fixture gets rather than raising out of the transport handler.
    """
    parts = url.path.rstrip("/").split("/")
    if len(parts) < 3:
        return None
    filter_id, region, filename = parts[-3], parts[-2], parts[-1]
    if filename.startswith("index_"):
        return f"{filter_id}_{region}_{filename}"
    return filename


def forecast_fixture_issue_date(
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR, *, past_days: int = FORECAST_PAST_DAYS
) -> date:
    """The issue date the recorded forecast fixture actually represents.

    Offline mode serves one fixed forecast snapshot, so pinning an *arbitrary* issue date labels
    the vintage with a time it does not describe -- a vintage whose targets sit years past its own
    ``issue_time`` while claiming a seven-day horizon. Deriving the date from the fixture instead
    keeps the seeded vintage internally consistent, and keeps it consistent automatically when the
    fixture is re-recorded.
    """
    path = fixtures_dir / FORECAST_FIXTURE
    try:
        payload = json.loads(path.read_bytes())
    except FileNotFoundError:
        raise FileNotFoundError(
            f"no recorded forecast fixture at {path}; "
            "record one with scripts/record_open_meteo_fixtures.py"
        ) from None
    times = payload.get("hourly", {}).get("time")
    if not times:
        raise ValueError(f"forecast fixture {path} has no hourly 'time' array")
    first_target = datetime.fromtimestamp(int(min(times)), UTC).date()
    return first_target + timedelta(days=past_days)


def open_meteo_fixture_name(url: httpx.URL) -> str | None:
    """Map an Open-Meteo request URL to its fixture filename, or ``None`` if unroutable.

    Archive requests key off their ``start_date`` / ``end_date`` query (one file per requested
    window); the forecast endpoint has a single current-snapshot fixture.
    """
    if url.path.endswith("/archive"):
        start = url.params.get("start_date")
        end = url.params.get("end_date")
        return f"open_meteo_archive_{start}_{end}.json"
    if url.path.endswith("/forecast"):
        return "open_meteo_forecast.json"
    return None


def _fixture_name(url: httpx.URL) -> str | None:
    """Route any connector URL to its fixture filename (Open-Meteo first, else SMARD)."""
    if url.path.endswith(("/archive", "/forecast")):
        return open_meteo_fixture_name(url)
    return smard_fixture_name(url)


def offline_transport(fixtures_dir: Path = DEFAULT_FIXTURES_DIR) -> httpx.MockTransport:
    """An ``httpx.MockTransport`` serving SMARD + Open-Meteo fixtures from ``fixtures_dir``.

    Injectable into any ``httpx.Client`` the connectors use; a missing fixture yields a ``404``
    naming the file, so an uncovered date range surfaces immediately rather than silently.

    Raises if ``fixtures_dir`` is not a directory. The default is derived from this file's location
    and is only meaningful inside a source checkout, so an installed-wheel run would otherwise 404
    on every request with a confusing path instead of saying what to do about it.
    """
    if not fixtures_dir.is_dir():
        raise NotADirectoryError(
            f"offline fixtures directory not found: {fixtures_dir} -- pass --fixtures-dir "
            "(the recorded fixtures ship with the source checkout, not the installed package)"
        )

    def handler(request: httpx.Request) -> httpx.Response:
        name = _fixture_name(request.url)
        if name is None:
            return httpx.Response(404, text=f"unroutable offline URL: {request.url}")
        path = fixtures_dir / name
        if not path.exists():
            return httpx.Response(404, text=f"no fixture: {path.name}")
        return httpx.Response(
            200, content=path.read_bytes(), headers={"content-type": "application/json"}
        )

    return httpx.MockTransport(handler)
