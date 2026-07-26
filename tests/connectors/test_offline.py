"""Tests for the offline fixture transport: directory validation and URL routing.

``offline.py`` is the seam every ``--offline`` run passes through -- CI rebuilds the whole
warehouse from it -- so its two failure modes have to be unmistakable rather than mysterious: a
fixtures directory that is not there, and a URL no recorded fixture answers. Both must name what
went wrong, and neither may reach the network.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from energy_platform.connectors.offline import (
    offline_transport,
    open_meteo_fixture_name,
    smard_fixture_name,
)
from energy_platform.connectors.smard import SmardClient, SmardError
from energy_platform.connectors.types import Dataset, Resolution
from energy_platform.orchestration.ingest import berlin_day_window

FIXTURES = Path(__file__).parent / "fixtures"

SMARD_BASE = "https://www.smard.de/app/chart_data"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def _get(fixtures_dir: Path, url: str) -> httpx.Response:
    """One request through the offline transport -- no network, whatever the URL."""
    with httpx.Client(transport=offline_transport(fixtures_dir)) as client:
        return client.get(url)


# -- Fixtures-directory validation -----------------------------------------------


def test_missing_fixtures_dir_is_rejected_by_name(tmp_path: Path) -> None:
    """A non-existent directory fails at construction, naming the path and the way out.

    The default is derived from this package's location and only means anything inside a source
    checkout, so an installed-wheel run would otherwise 404 on every single request with a path
    the user has no way to interpret.
    """
    missing = tmp_path / "nowhere"
    with pytest.raises(NotADirectoryError) as excinfo:
        offline_transport(missing)

    message = str(excinfo.value)
    assert str(missing) in message
    assert "--fixtures-dir" in message


def test_fixtures_dir_that_is_a_file_is_rejected(tmp_path: Path) -> None:
    """A path that exists but is not a directory hits the same guard, not a later stranger one."""
    not_a_dir = tmp_path / "fixtures.json"
    not_a_dir.write_text("{}")
    with pytest.raises(NotADirectoryError) as excinfo:
        offline_transport(not_a_dir)

    assert str(not_a_dir) in str(excinfo.value)


def test_empty_fixtures_dir_serves_404_naming_the_missing_file(tmp_path: Path) -> None:
    """An empty directory is valid but answers nothing: each 404 names the fixture it wanted.

    This is the coverage-gap path -- an uncovered date range -- and it must stay distinguishable
    from the unroutable-URL path below, since the fix differs (record a fixture vs fix the URL).
    """
    response = _get(tmp_path, f"{SMARD_BASE}/4169/DE/index_hour.json")

    assert response.status_code == 404
    assert response.text == "no fixture: 4169_DE_index_hour.json"


def test_valid_fixtures_dir_serves_the_recorded_bytes() -> None:
    """The happy path: a recorded fixture is returned verbatim, as JSON."""
    response = _get(FIXTURES, f"{SMARD_BASE}/4169/DE/index_hour.json")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == (FIXTURES / "4169_DE_index_hour.json").read_bytes()
    assert "timestamps" in response.json()


# -- URL routing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # ``.../{filter}/{region}/index_{res}.json`` folds the path into the filename ...
        (f"{SMARD_BASE}/4169/DE/index_hour.json", "4169_DE_index_hour.json"),
        (f"{SMARD_BASE}/4169/DE/index_quarterhour.json", "4169_DE_index_quarterhour.json"),
        # ... while a weekly file already embeds {filter}_{region}_{res}_{ts} and is used as-is.
        (
            f"{SMARD_BASE}/4169/DE/4169_DE_hour_1711321200000.json",
            "4169_DE_hour_1711321200000.json",
        ),
        (f"{SMARD_BASE}/410/DE/410_DE_hour_1730070000000.json", "410_DE_hour_1730070000000.json"),
    ],
)
def test_smard_urls_map_to_recorded_filenames(url: str, expected: str) -> None:
    assert smard_fixture_name(httpx.URL(url)) == expected
    assert (FIXTURES / expected).exists(), "the routed fixture must actually be recorded"


def test_open_meteo_urls_map_to_recorded_filenames() -> None:
    """Archive requests key off their requested window; the forecast has one snapshot fixture."""
    archive = httpx.URL(f"{OPEN_METEO_ARCHIVE}?start_date=2024-03-30&end_date=2024-03-31")
    assert open_meteo_fixture_name(archive) == "open_meteo_archive_2024-03-30_2024-03-31.json"

    forecast = httpx.URL("https://api.open-meteo.com/v1/forecast?forecast_days=7")
    assert open_meteo_fixture_name(forecast) == "open_meteo_forecast.json"


@pytest.mark.parametrize("path", ["/foo.json", "/index_hour.json", "/"])
def test_short_paths_are_unroutable_rather_than_a_crash(path: str) -> None:
    """A URL with too few segments cannot be a chart-data request.

    It used to index ``parts[-3]`` unconditionally and raise ``IndexError`` from inside the
    transport handler -- an opaque traceback where the module promises a deliberate 404.
    """
    assert smard_fixture_name(httpx.URL(f"https://host{path}")) is None


def test_unroutable_url_is_refused_and_names_the_url() -> None:
    """No fall-through: an unrecognised URL is refused by the transport, quoting the URL back."""
    url = f"https://host{'/foo.json'}"
    response = _get(FIXTURES, url)

    assert response.status_code == 404
    assert response.text == f"unroutable offline URL: {url}"


def test_refused_request_surfaces_as_a_client_error_naming_the_url() -> None:
    """Through a real connector a refusal is an error, not a silently empty result.

    404 is a permanent 4xx, so the client raises instead of retrying, and the message carries the
    URL that could not be served -- an offline run stops at its first uncovered request rather
    than continuing with nothing. Driven through the public ``fetch_window`` for a region no
    fixture was recorded for.
    """
    with httpx.Client(transport=offline_transport(FIXTURES)) as http:
        client = SmardClient(http, base_url=SMARD_BASE, max_retries=0, sleep=lambda _: None)
        with pytest.raises(SmardError) as excinfo:
            client.fetch_window(
                Dataset.DAY_AHEAD_PRICE,
                "XX",
                Resolution.HOUR,
                berlin_day_window(date(2024, 3, 31)),
            )

    assert f"{SMARD_BASE}/4169/XX/index_hour.json" in str(excinfo.value)
