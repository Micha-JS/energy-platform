"""Tests for the SMARD client: parsing, window slicing, DST, and week straddling.

All I/O is served from recorded fixtures or synthetic in-test payloads -- no live calls.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from energy_platform.connectors.smard import SmardClient, SmardError
from energy_platform.connectors.types import Dataset, Resolution
from energy_platform.orchestration.ingest import berlin_day_window

NORMAL_DAY = date(2024, 6, 12)
MARCH_DST_DAY = date(2024, 3, 31)  # last Sunday of March -> 23 hours
OCTOBER_DST_DAY = date(2024, 10, 27)  # last Sunday of October -> 25 hours


def _fetch(client: SmardClient, dataset: Dataset, resolution: Resolution, day: date) -> int:
    series = client.fetch_window(dataset, "DE", resolution, berlin_day_window(day))
    return series.row_count


@pytest.mark.parametrize(
    ("dataset", "day", "expected"),
    [
        (Dataset.DAY_AHEAD_PRICE, NORMAL_DAY, 24),
        (Dataset.DAY_AHEAD_PRICE, MARCH_DST_DAY, 23),
        (Dataset.DAY_AHEAD_PRICE, OCTOBER_DST_DAY, 25),
        (Dataset.GRID_LOAD, NORMAL_DAY, 24),
        (Dataset.GRID_LOAD, MARCH_DST_DAY, 23),
        (Dataset.GRID_LOAD, OCTOBER_DST_DAY, 25),
    ],
)
def test_hourly_window_counts_including_dst(
    smard_client: SmardClient, dataset: Dataset, day: date, expected: int
) -> None:
    assert _fetch(smard_client, dataset, Resolution.HOUR, day) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (MARCH_DST_DAY, 92),  # 23 hours * 4
        (OCTOBER_DST_DAY, 100),  # 25 hours * 4
    ],
)
def test_quarterhour_window_counts_including_dst(
    smard_client: SmardClient, day: date, expected: int
) -> None:
    assert _fetch(smard_client, Dataset.DAY_AHEAD_PRICE, Resolution.QUARTERHOUR, day) == expected


def test_window_bounds_are_half_open(smard_client: SmardClient) -> None:
    """Every returned instant lies inside the requested window; no leakage across days."""
    window = berlin_day_window(NORMAL_DAY)
    series = smard_client.fetch_window(Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, window)
    assert all(window.contains(ts) for ts, _ in series.points)
    assert series.points == tuple(sorted(series.points))


def test_source_metadata_is_populated(smard_client: SmardClient) -> None:
    window = berlin_day_window(NORMAL_DAY)
    series = smard_client.fetch_window(Dataset.GRID_LOAD, "DE", Resolution.HOUR, window)
    assert series.source == "smard"
    assert series.source_tz == "Europe/Berlin"
    assert len(series.source_urls) >= 1
    assert all(url.endswith(".json") for url in series.source_urls)


# -- Synthetic payloads for parsing + straddle edge cases ------------------------


def _synthetic_client(files: dict[str, bytes]) -> SmardClient:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rstrip("/").split("/")[-1]
        if name not in files:
            return httpx.Response(404)
        return httpx.Response(200, content=files[name])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SmardClient(http, max_retries=0, sleep=lambda _: None)


def test_null_values_are_preserved_not_fabricated() -> None:
    """A source ``null`` becomes ``None`` -- never dropped, zeroed, or interpolated."""
    week_ts = 1_000_000
    index = json.dumps({"timestamps": [week_ts]}).encode()
    series = json.dumps(
        {"series": [[1_000_000, 10.0], [1_003_600_000, None], [1_007_200_000, 12.5]]}
    ).encode()
    client = _synthetic_client(
        {
            "index_hour.json": index,
            f"4169_DE_hour_{week_ts}.json": series,
        }
    )
    from energy_platform.connectors.types import UtcWindow

    result = client.fetch_window(
        Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, UtcWindow(0, 2_000_000_000)
    )
    values = [value for _, value in result.points]
    assert values == [10.0, None, 12.5]
    assert result.null_count == 1


def test_window_straddling_two_week_files_is_stitched() -> None:
    """A window spanning a week boundary concatenates both files and records both URLs."""
    from energy_platform.connectors.types import UtcWindow

    week_a, week_b = 1_000_000, 2_000_000
    index = json.dumps({"timestamps": [week_a, week_b]}).encode()
    file_a = json.dumps({"series": [[1_000_000, 1.0], [1_500_000, 2.0], [1_900_000, 3.0]]}).encode()
    file_b = json.dumps({"series": [[2_000_000, 4.0], [2_300_000, 5.0]]}).encode()
    client = _synthetic_client(
        {
            "index_hour.json": index,
            f"4169_DE_hour_{week_a}.json": file_a,
            f"4169_DE_hour_{week_b}.json": file_b,
        }
    )
    result = client.fetch_window(
        Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, UtcWindow(1_400_000, 2_100_000)
    )
    assert [ts for ts, _ in result.points] == [1_500_000, 1_900_000, 2_000_000]
    assert len(result.source_urls) == 2


# -- Transient failure handling in the bounded-retry loop ------------------------


def test_rate_limit_429_is_retried_then_succeeds() -> None:
    """A 429 (rate limit) backs off and retries rather than aborting the partition."""
    from energy_platform.connectors.types import UtcWindow

    week_ts = 1_000_000
    files = {
        "index_hour.json": json.dumps({"timestamps": [week_ts]}).encode(),
        f"4169_DE_hour_{week_ts}.json": json.dumps({"series": [[1_000_000, 10.0]]}).encode(),
    }
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:  # first request is rate-limited
            return httpx.Response(429)
        name = request.url.path.rstrip("/").split("/")[-1]
        return httpx.Response(200, content=files[name])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = SmardClient(http, max_retries=2, sleep=lambda _: None)
    result = client.fetch_window(
        Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, UtcWindow(0, 2_000_000_000)
    )
    assert [value for _, value in result.points] == [10.0]
    assert state["n"] >= 2  # the 429 was retried, not surfaced


def test_non_json_200_body_raises_smard_error() -> None:
    """A 200 with a non-JSON body is wrapped as a domain error, not a bare JSONDecodeError."""
    from energy_platform.connectors.types import UtcWindow

    index = json.dumps({"timestamps": [1_000_000]}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rstrip("/").split("/")[-1]
        if name == "index_hour.json":
            return httpx.Response(200, content=index)
        return httpx.Response(200, content=b"<html>maintenance</html>")  # non-JSON week file

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = SmardClient(http, max_retries=0, sleep=lambda _: None)
    with pytest.raises(SmardError):
        client.fetch_window(
            Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, UtcWindow(0, 2_000_000_000)
        )
