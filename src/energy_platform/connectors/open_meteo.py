"""Typed clients for the Open-Meteo weather API -- archive (actuals) and forecast.

Open-Meteo serves weather as JSON with parallel hourly arrays: a ``time`` array plus one
array per requested variable. Both clients request every weather variable in a single call
(so a multi-variable ingestion hits the API once) and ask for values in UTC directly
(``timeformat=unixtime&timezone=UTC``), so no timezone conversion happens in the client --
the epoch-second timestamps are UTC instants, matching :class:`Point`.

Two truths, two clients:

* :class:`OpenMeteoArchiveClient` -- historical *actuals* (ERA5-backed). Windowed like SMARD;
  it satisfies the :class:`~energy_platform.connectors.base.MarketDataConnector` protocol, so
  the existing ``ingest_partition`` core ingests weather actuals with no change.
* :class:`OpenMeteoForecastClient` -- the *current* forecast horizon, captured as one vintage.
  The forecast API only ever serves the current issue, so forecasts cannot be backfilled;
  they accrue forward, one immutable snapshot per day.

No API key is required (CC BY 4.0). See https://open-meteo.com/en/docs.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import httpx

from energy_platform.connectors.types import (
    Dataset,
    Point,
    RawForecast,
    RawSeries,
    Resolution,
    UtcWindow,
)

SOURCE: Final = "open_meteo"
# Values are requested and returned in UTC; partitions still key off Berlin calendar days.
SOURCE_TZ: Final = "UTC"

# Sent on every Open-Meteo request. Shared by the Dagster resources and the CLI.
USER_AGENT: Final = "energy-platform/0.1 (+https://github.com; Open-Meteo connector)"

# Public API base URLs. Duplicated as defaults in ``config`` (whence the resources/CLI read
# them), mirroring how ``smard`` keeps its own module default -- the connector stays
# self-contained and importable without the config layer.
DEFAULT_ARCHIVE_URL: Final = "https://archive-api.open-meteo.com/v1/archive"
DEFAULT_FORECAST_URL: Final = "https://api.open-meteo.com/v1/forecast"

# The weather variables we ingest, in a stable request order. Each ``Dataset`` value doubles
# as the Open-Meteo ``hourly`` parameter name, so no separate name mapping is needed.
WEATHER_VARIABLES: Final[tuple[Dataset, ...]] = (
    Dataset.SHORTWAVE_RADIATION,
    Dataset.DIRECT_RADIATION,
    Dataset.DIFFUSE_RADIATION,
    Dataset.TEMPERATURE_2M,
    Dataset.CLOUD_COVER,
    Dataset.WIND_SPEED_10M,
)

# Retry only on transient failures; 4xx (e.g. a bad-parameter 400) are permanent. Matches the
# SMARD client's policy so a rapid backfill that gets rate-limited backs off and retries.
_RETRYABLE_STATUS: Final = frozenset({408, 429, 500, 502, 503, 504})

# site id -> (latitude, longitude). Kept as plain floats so the connector needs no config import.
Coordinates = Mapping[str, tuple[float, float]]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class OpenMeteoError(RuntimeError):
    """Raised when Open-Meteo data cannot be retrieved or parsed."""


class _OpenMeteoBase:
    """Shared HTTP, retry, coordinate lookup, and hourly-array parsing for both clients.

    The ``httpx.Client`` is injected so tests supply an ``httpx.MockTransport`` backed by
    recorded fixtures -- CI never touches the live API.
    """

    source = SOURCE

    def __init__(
        self,
        http: httpx.Client,
        base_url: str,
        coordinates: Coordinates,
        *,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._coordinates = dict(coordinates)
        self._max_retries = max_retries
        self._sleep = sleep

    def _coords(self, site_id: str) -> tuple[float, float]:
        try:
            return self._coordinates[site_id]
        except KeyError:
            known = ", ".join(sorted(self._coordinates)) or "<none>"
            raise OpenMeteoError(
                f"no coordinates configured for site '{site_id}'; known: {known}"
            ) from None

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.get(url, params=params)
                if response.status_code in _RETRYABLE_STATUS:
                    raise OpenMeteoError(f"Open-Meteo returned {response.status_code} for {url}")
                response.raise_for_status()
                try:
                    data: Any = response.json()
                except json.JSONDecodeError as exc:
                    # A 200 with a non-JSON body (an HTML maintenance/CDN page) is transient --
                    # raise a domain error so the retry clause backs off instead of propagating.
                    raise OpenMeteoError(f"Open-Meteo returned a non-JSON body for {url}") from exc
                if not isinstance(data, dict):
                    raise OpenMeteoError(f"Open-Meteo returned a non-object JSON body for {url}")
                result: dict[str, Any] = data
                return result
            except (httpx.TransportError, OpenMeteoError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    self._sleep(0.5 * (2**attempt))
            except httpx.HTTPStatusError as exc:  # 4xx -- permanent
                raise OpenMeteoError(f"Open-Meteo request failed: {url}") from exc
        raise OpenMeteoError(f"Open-Meteo request failed after retries: {url}") from last_exc

    def _parse_hourly(
        self, data: dict[str, Any], variables: Sequence[Dataset]
    ) -> dict[Dataset, tuple[Point, ...]]:
        """Zip the shared ``time`` array with each variable's array into per-variable points."""
        hourly = data.get("hourly")
        if not isinstance(hourly, dict):
            raise OpenMeteoError("malformed response: missing 'hourly' object")
        raw_time = hourly.get("time")
        if not isinstance(raw_time, list):
            raise OpenMeteoError("malformed response: missing hourly 'time' array")
        times_ms = [int(t) * 1000 for t in raw_time]

        series: dict[Dataset, tuple[Point, ...]] = {}
        for variable in variables:
            values = hourly.get(variable.value)
            if not isinstance(values, list):
                raise OpenMeteoError(f"malformed response: missing hourly '{variable.value}' array")
            if len(values) != len(times_ms):
                raise OpenMeteoError(
                    f"length mismatch for '{variable.value}': "
                    f"{len(values)} values vs {len(times_ms)} timestamps"
                )
            series[variable] = tuple(
                (ts, _parse_value(value)) for ts, value in zip(times_ms, values, strict=True)
            )
        return series


class OpenMeteoArchiveClient(_OpenMeteoBase):
    """Historical weather actuals from the Open-Meteo archive (ERA5).

    Fetches every variable for the UTC-date range covering a requested window in one call
    (memoised, so a multi-year, multi-variable backfill fetches each date-range once), then
    slices the requested variable to the window -- mirroring SMARD's fetch-file-then-slice.
    """

    def __init__(
        self,
        http: httpx.Client,
        coordinates: Coordinates,
        *,
        base_url: str = DEFAULT_ARCHIVE_URL,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(http, base_url, coordinates, max_retries=max_retries, sleep=sleep)
        # Memoised per (site, start_date, end_date); holds all variables for that range.
        self._cache: dict[tuple[str, str, str], dict[Dataset, tuple[Point, ...]]] = {}

    def fetch_window(
        self,
        dataset: Dataset,
        region: str,
        resolution: Resolution,
        window: UtcWindow,
    ) -> RawSeries:
        # The archive addresses whole UTC calendar dates; a Berlin-day window straddles two of
        # them, so request the covering [start, end] UTC dates and slice back to the window.
        start_date = _utc_date(window.start_ms)
        end_date = _utc_date(window.end_ms - 1)  # half-open -> last instant inside the window
        all_series = self._fetch_range(region, start_date, end_date)

        points = tuple(point for point in all_series[dataset] if window.contains(point[0]))
        return RawSeries(
            source=SOURCE,
            dataset=dataset,
            region=region,
            resolution=resolution,
            source_tz=SOURCE_TZ,
            source_urls=(self._range_url(region, start_date, end_date),),
            points=points,
        )

    def _fetch_range(
        self, site_id: str, start_date: date, end_date: date
    ) -> dict[Dataset, tuple[Point, ...]]:
        key = (site_id, start_date.isoformat(), end_date.isoformat())
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        lat, lon = self._coords(site_id)
        params = _archive_params(lat, lon, start_date, end_date)
        data = self._get_json(self._base_url, params)
        series = self._parse_hourly(data, WEATHER_VARIABLES)
        self._cache[key] = series
        return series

    def _range_url(self, site_id: str, start_date: date, end_date: date) -> str:
        lat, lon = self._coords(site_id)
        return str(
            httpx.URL(self._base_url, params=_archive_params(lat, lon, start_date, end_date))
        )


class OpenMeteoForecastClient(_OpenMeteoBase):
    """The current forecast horizon for a site, captured as one immutable vintage.

    ``past_days`` pulls a small slice before the issue instant so the issue day's earliest
    hours are present; ``forecast_days`` sets how far ahead the vintage reaches.
    """

    def __init__(
        self,
        http: httpx.Client,
        coordinates: Coordinates,
        *,
        base_url: str = DEFAULT_FORECAST_URL,
        forecast_days: int = 7,
        past_days: int = 1,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(http, base_url, coordinates, max_retries=max_retries, sleep=sleep)
        self._forecast_days = forecast_days
        self._past_days = past_days

    @property
    def forecast_days(self) -> int:
        return self._forecast_days

    def fetch_forecast(
        self,
        site_id: str,
        resolution: Resolution = Resolution.HOUR,
        variables: Sequence[Dataset] = WEATHER_VARIABLES,
    ) -> RawForecast:
        lat, lon = self._coords(site_id)
        params = _forecast_params(lat, lon, self._forecast_days, self._past_days)
        data = self._get_json(self._base_url, params)
        series = self._parse_hourly(data, variables)
        return RawForecast(
            source=SOURCE,
            region=site_id,
            resolution=resolution,
            source_tz=SOURCE_TZ,
            source_urls=(str(httpx.URL(self._base_url, params=params)),),
            series=series,
        )


def _hourly_params(lat: float, lon: float) -> dict[str, Any]:
    return {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(variable.value for variable in WEATHER_VARIABLES),
        "timezone": "UTC",
        "timeformat": "unixtime",
    }


def _archive_params(lat: float, lon: float, start_date: date, end_date: date) -> dict[str, Any]:
    return {
        **_hourly_params(lat, lon),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def _forecast_params(lat: float, lon: float, forecast_days: int, past_days: int) -> dict[str, Any]:
    return {
        **_hourly_params(lat, lon),
        "forecast_days": forecast_days,
        "past_days": past_days,
    }


def _utc_date(ts_ms: int) -> date:
    return (_EPOCH + timedelta(milliseconds=ts_ms)).date()


def _parse_value(value: object) -> float | None:
    """A source ``null`` becomes ``None`` -- never dropped, zeroed, or interpolated."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raise OpenMeteoError(f"expected a numeric hourly value or null, got {value!r}")
