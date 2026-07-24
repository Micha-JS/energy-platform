"""Typed client for the SMARD (Bundesnetzagentur) chart-data JSON API.

SMARD publishes market data as weekly JSON files addressed by an ``epoch_ms`` start
timestamp. The client resolves which week file(s) cover a requested UTC window, fetches
them (memoised so a multi-year backfill downloads each weekly file at most once), and
returns the series sliced to the window with every value preserved as received.

No API key is required. See https://github.com/bundesAPI/smard-api for the endpoint map.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Any, Final

import httpx

from energy_platform.connectors.types import (
    Dataset,
    Point,
    RawSeries,
    Resolution,
    UtcWindow,
)

SOURCE: Final = "smard"
SOURCE_TZ: Final = "Europe/Berlin"

# Sent on every SMARD request. Shared by the Dagster resource and the backfill CLI so the
# contact string is defined once.
USER_AGENT: Final = "energy-platform/0.1 (+https://github.com; SMARD connector)"

# SMARD filter IDs. Prices are the Germany/Luxembourg day-ahead wholesale spot price;
# grid load is total consumption (Netzlast).
FILTERS: Final[dict[Dataset, str]] = {
    Dataset.DAY_AHEAD_PRICE: "4169",
    Dataset.GRID_LOAD: "410",
}

# Retry only on transient failures. Most 4xx are permanent and re-raised immediately;
# 408 (Request Timeout) and 429 (Too Many Requests) are the transient exceptions -- a
# rapid backfill can be rate-limited, and backing off then retrying is the correct response.
_RETRYABLE_STATUS: Final = frozenset({408, 429, 500, 502, 503, 504})


class SmardError(RuntimeError):
    """Raised when SMARD data cannot be retrieved or parsed."""


class SmardClient:
    """Fetches SMARD series for a UTC window.

    The ``httpx.Client`` is injected so tests can supply an ``httpx.MockTransport`` backed
    by recorded fixtures -- CI never touches the live API.
    """

    source = SOURCE

    def __init__(
        self,
        http: httpx.Client,
        *,
        base_url: str = "https://www.smard.de/app/chart_data",
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleep = sleep
        # Memoisation keyed by endpoint identity.
        self._index_cache: dict[tuple[str, str, Resolution], tuple[int, ...]] = {}
        self._week_cache: dict[tuple[str, str, Resolution, int], tuple[Point, ...]] = {}

    # -- URL construction ---------------------------------------------------------

    def _index_url(self, filter_id: str, region: str, resolution: Resolution) -> str:
        return f"{self._base_url}/{filter_id}/{region}/index_{resolution.value}.json"

    def _week_url(self, filter_id: str, region: str, resolution: Resolution, week_ts: int) -> str:
        name = f"{filter_id}_{region}_{resolution.value}_{week_ts}.json"
        return f"{self._base_url}/{filter_id}/{region}/{name}"

    # -- HTTP with bounded retry --------------------------------------------------

    def _get_json(self, url: str) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.get(url)
                if response.status_code in _RETRYABLE_STATUS:
                    raise SmardError(f"SMARD returned {response.status_code} for {url}")
                response.raise_for_status()
                try:
                    data: Any = response.json()
                except json.JSONDecodeError as exc:
                    # A 200 with a non-JSON body (e.g. an HTML maintenance/CDN page) is a
                    # transient failure, not a permanent one -- raise a domain error so the
                    # retry clause below backs off and retries instead of propagating raw.
                    raise SmardError(f"SMARD returned a non-JSON body for {url}") from exc
                if not isinstance(data, dict):
                    # A well-formed but unexpected JSON shape (array/scalar) would otherwise
                    # blow up later with a raw AttributeError on ``.get``; convert it here so
                    # every failure path leaves as a ``SmardError``.
                    raise SmardError(f"SMARD returned a non-object JSON body for {url}")
                result: dict[str, Any] = data
                return result
            except (httpx.TransportError, SmardError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    self._sleep(0.5 * (2**attempt))
            except httpx.HTTPStatusError as exc:  # 4xx -- permanent
                raise SmardError(f"SMARD request failed: {url}") from exc
        raise SmardError(f"SMARD request failed after retries: {url}") from last_exc

    # -- Endpoints ----------------------------------------------------------------

    def available_timestamps(
        self, filter_id: str, region: str, resolution: Resolution
    ) -> tuple[int, ...]:
        """Return the sorted weekly-aligned start timestamps SMARD offers (memoised)."""
        key = (filter_id, region, resolution)
        cached = self._index_cache.get(key)
        if cached is not None:
            return cached
        data = self._get_json(self._index_url(filter_id, region, resolution))
        raw = data.get("timestamps")
        if not isinstance(raw, list):
            raise SmardError(f"malformed index response for {key}: missing 'timestamps'")
        timestamps = tuple(sorted(int(ts) for ts in raw))
        self._index_cache[key] = timestamps
        return timestamps

    def fetch_week(
        self, filter_id: str, region: str, resolution: Resolution, week_ts: int
    ) -> tuple[Point, ...]:
        """Return one weekly file's series as ``(epoch_ms, value|null)`` points (memoised)."""
        key = (filter_id, region, resolution, week_ts)
        cached = self._week_cache.get(key)
        if cached is not None:
            return cached
        data = self._get_json(self._week_url(filter_id, region, resolution, week_ts))
        series = data.get("series")
        if not isinstance(series, list):
            raise SmardError(f"malformed series response for {key}: missing 'series'")
        points = tuple(_parse_point(item) for item in series)
        self._week_cache[key] = points
        return points

    # -- Public API ---------------------------------------------------------------

    def fetch_window(
        self,
        dataset: Dataset,
        region: str,
        resolution: Resolution,
        window: UtcWindow,
    ) -> RawSeries:
        filter_id = FILTERS[dataset]
        weeks = self._weeks_covering(filter_id, region, resolution, window)

        merged: dict[int, float | None] = {}
        used_urls: list[str] = []
        for week_ts in weeks:
            used_urls.append(self._week_url(filter_id, region, resolution, week_ts))
            for ts, value in self.fetch_week(filter_id, region, resolution, week_ts):
                if window.contains(ts):
                    merged[ts] = value

        points: tuple[Point, ...] = tuple((ts, merged[ts]) for ts in sorted(merged))
        return RawSeries(
            source=SOURCE,
            dataset=dataset,
            region=region,
            resolution=resolution,
            source_tz=SOURCE_TZ,
            source_urls=tuple(used_urls),
            points=points,
        )

    def _weeks_covering(
        self, filter_id: str, region: str, resolution: Resolution, window: UtcWindow
    ) -> Sequence[int]:
        """Weekly start timestamps whose files can contain points inside ``window``.

        A file starting at ``w`` covers ``[w, next_w)``, so we take the last week starting
        at or before the window start plus every later week starting before the window end.
        """
        timestamps = self.available_timestamps(filter_id, region, resolution)
        if not timestamps:
            return ()
        start_idx = _last_le(timestamps, window.start_ms)
        return [ts for ts in timestamps[start_idx:] if ts < window.end_ms]


def _parse_point(item: object) -> Point:
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        raise SmardError(f"malformed series point: {item!r}")
    ts, value = item
    parsed_value = None if value is None else float(value)
    return int(ts), parsed_value


def _last_le(timestamps: tuple[int, ...], target: int) -> int:
    """Index of the greatest timestamp <= target, or 0 if all exceed it."""
    import bisect

    idx = bisect.bisect_right(timestamps, target) - 1
    return max(idx, 0)
