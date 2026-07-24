"""Tests for the Open-Meteo clients: parsing, window slicing, DST, memoisation, forecasts.

All I/O is served from offline fixtures or synthetic in-test payloads -- no live calls.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import httpx
import pytest

from energy_platform.connectors.open_meteo import (
    WEATHER_VARIABLES,
    OpenMeteoArchiveClient,
    OpenMeteoError,
    OpenMeteoForecastClient,
)
from energy_platform.connectors.types import Dataset, Resolution, UtcWindow
from energy_platform.orchestration.ingest import berlin_day_window

COORDS = {"home": (52.52, 13.40)}
FIXTURES = Path(__file__).parent / "fixtures"

NORMAL_DAY = date(2024, 6, 12)
MARCH_DST_DAY = date(2024, 3, 31)  # last Sunday of March -> 23 hours
OCTOBER_DST_DAY = date(2024, 10, 27)  # last Sunday of October -> 25 hours


# -- Archive: window slicing, DST, nulls, metadata -------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [(NORMAL_DAY, 24), (MARCH_DST_DAY, 23), (OCTOBER_DST_DAY, 25)],
)
def test_archive_window_counts_including_dst(
    open_meteo_archive_client: OpenMeteoArchiveClient, day: date, expected: int
) -> None:
    """A Berlin-day window straddles two UTC dates; slicing yields 23/24/25 hours across DST."""
    series = open_meteo_archive_client.fetch_window(
        Dataset.TEMPERATURE_2M, "home", Resolution.HOUR, berlin_day_window(day)
    )
    assert series.row_count == expected


def test_archive_preserves_null_values_not_fabricated(
    open_meteo_archive_client: OpenMeteoArchiveClient,
) -> None:
    """A source ``null`` becomes ``None`` -- never dropped, zeroed, or interpolated."""
    window = berlin_day_window(NORMAL_DAY)
    shortwave = open_meteo_archive_client.fetch_window(
        Dataset.SHORTWAVE_RADIATION, "home", Resolution.HOUR, window
    )
    assert shortwave.null_count == 1
    assert any(value is None for _, value in shortwave.points)

    # A variable with no gap keeps its full complement -- the null is per-variable, not global.
    temperature = open_meteo_archive_client.fetch_window(
        Dataset.TEMPERATURE_2M, "home", Resolution.HOUR, window
    )
    assert temperature.null_count == 0


def test_archive_window_bounds_are_half_open_and_sorted(
    open_meteo_archive_client: OpenMeteoArchiveClient,
) -> None:
    window = berlin_day_window(NORMAL_DAY)
    series = open_meteo_archive_client.fetch_window(
        Dataset.WIND_SPEED_10M, "home", Resolution.HOUR, window
    )
    assert all(window.contains(ts) for ts, _ in series.points)
    assert series.points == tuple(sorted(series.points))


def test_archive_source_metadata_is_populated(
    open_meteo_archive_client: OpenMeteoArchiveClient,
) -> None:
    series = open_meteo_archive_client.fetch_window(
        Dataset.CLOUD_COVER, "home", Resolution.HOUR, berlin_day_window(NORMAL_DAY)
    )
    assert series.source == "open_meteo"
    assert series.source_tz == "UTC"
    assert len(series.source_urls) == 1
    assert "archive" in series.source_urls[0]


def test_all_variables_share_one_memoised_http_call() -> None:
    """Fetching every variable for a day hits the API once -- the response is memoised."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        name = (
            f"open_meteo_archive_{request.url.params.get('start_date')}"
            f"_{request.url.params.get('end_date')}.json"
        )
        return httpx.Response(
            200,
            content=(FIXTURES / name).read_bytes(),
            headers={"content-type": "application/json"},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenMeteoArchiveClient(http, COORDS, max_retries=0, sleep=lambda _: None)
    window = berlin_day_window(NORMAL_DAY)
    for variable in WEATHER_VARIABLES:
        client.fetch_window(variable, "home", Resolution.HOUR, window)
    assert calls["n"] == 1


def test_unknown_site_raises(open_meteo_archive_client: OpenMeteoArchiveClient) -> None:
    with pytest.raises(OpenMeteoError, match="no coordinates"):
        open_meteo_archive_client.fetch_window(
            Dataset.TEMPERATURE_2M, "atlantis", Resolution.HOUR, berlin_day_window(NORMAL_DAY)
        )


# -- Forecast: full horizon, all variables, nulls preserved ----------------------


def test_forecast_returns_all_variables_over_the_horizon(
    open_meteo_forecast_client: OpenMeteoForecastClient,
) -> None:
    forecast = open_meteo_forecast_client.fetch_forecast("home")
    assert set(forecast.series) == set(WEATHER_VARIABLES)
    # past_days=1 + forecast_days=7 -> 8 UTC days * 24 h, identical length across variables.
    assert {len(points) for points in forecast.series.values()} == {192}
    assert forecast.source == "open_meteo"
    assert forecast.source_tz == "UTC"
    assert forecast.null_count == 1  # the injected forecast gap survives


def test_forecast_points_are_sorted_utc_instants(
    open_meteo_forecast_client: OpenMeteoForecastClient,
) -> None:
    forecast = open_meteo_forecast_client.fetch_forecast("home")
    for points in forecast.series.values():
        timestamps = [ts for ts, _ in points]
        assert timestamps == sorted(timestamps)


# -- Transient failure handling and malformed payloads ---------------------------

_WIDE_WINDOW = UtcWindow(0, 2_000_000_000_000)


def _hourly(**arrays: Sequence[object]) -> bytes:
    return json.dumps({"hourly": arrays}).encode()


def _full_hourly(time: list[int], value: float | None) -> bytes:
    variables: dict[str, Sequence[object]] = {
        v.value: [value] * len(time) for v in WEATHER_VARIABLES
    }
    return _hourly(time=time, **variables)


def test_rate_limit_429_is_retried_then_succeeds() -> None:
    body = _full_hourly([0], 1.0)
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenMeteoArchiveClient(http, COORDS, max_retries=2, sleep=lambda _: None)
    series = client.fetch_window(Dataset.TEMPERATURE_2M, "home", Resolution.HOUR, _WIDE_WINDOW)
    assert [value for _, value in series.points] == [1.0]
    assert state["n"] >= 2  # the 429 was retried, not surfaced


def test_non_json_200_body_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenMeteoArchiveClient(http, COORDS, max_retries=0, sleep=lambda _: None)
    with pytest.raises(OpenMeteoError):
        client.fetch_window(Dataset.TEMPERATURE_2M, "home", Resolution.HOUR, _WIDE_WINDOW)


def test_missing_variable_array_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_hourly(time=[0]), headers={"content-type": "application/json"}
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenMeteoArchiveClient(http, COORDS, max_retries=0, sleep=lambda _: None)
    with pytest.raises(OpenMeteoError, match="missing hourly"):
        client.fetch_window(Dataset.SHORTWAVE_RADIATION, "home", Resolution.HOUR, _WIDE_WINDOW)


def test_length_mismatch_raises() -> None:
    body = _hourly(time=[0, 3600], shortwave_radiation=[1.0])  # 1 value for 2 timestamps

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenMeteoArchiveClient(http, COORDS, max_retries=0, sleep=lambda _: None)
    with pytest.raises(OpenMeteoError, match="length mismatch"):
        client.fetch_window(Dataset.SHORTWAVE_RADIATION, "home", Resolution.HOUR, _WIDE_WINDOW)
