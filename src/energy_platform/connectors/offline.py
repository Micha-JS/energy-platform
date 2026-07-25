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

from pathlib import Path

import httpx

# ``.../src/energy_platform/connectors/offline.py`` -> repo root is three parents up from the
# package root; fixtures live under ``tests/connectors/fixtures``.
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "connectors" / "fixtures"


def smard_fixture_name(url: httpx.URL) -> str:
    """Map a SMARD request URL to its recorded fixture filename.

    ``.../{filter}/{region}/index_{res}.json`` -> ``{filter}_{region}_index_{res}.json``;
    weekly files already embed ``{filter}_{region}_{res}_{ts}`` and are used as-is.
    """
    parts = url.path.rstrip("/").split("/")
    filter_id, region, filename = parts[-3], parts[-2], parts[-1]
    if filename.startswith("index_"):
        return f"{filter_id}_{region}_{filename}"
    return filename


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
    """

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
