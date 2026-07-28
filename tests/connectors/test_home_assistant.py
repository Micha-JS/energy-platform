"""Home Assistant connector: offline conversion, error paths, and the read-only contract.

Every test is served from an ``httpx.MockTransport`` -- either a recorded, credential-free
fixture or a hand-built payload -- so CI never reaches a live house. The numeric assertions pin
the power-integration and level-sampling conversions; the rest pin the fail-loud error paths.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from energy_platform.connectors.home_assistant import HomeAssistantClient, HomeAssistantError
from energy_platform.connectors.types import Dataset, Resolution
from energy_platform.orchestration.ingest import berlin_day_window

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DAY = datetime(2024, 6, 15).date()
WINDOW = berlin_day_window(DAY)


def _client(
    payload: Any,
    entity_map: dict[Dataset, str],
    *,
    status: int = 200,
) -> HomeAssistantClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="boom")
        return httpx.Response(200, json=payload)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return HomeAssistantClient(
        http, "http://ha.local", entity_map, max_retries=0, sleep=lambda _: None
    )


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp() * 1000)


def _load_fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text())


# -- Conversion (recorded fixture) -----------------------------------------------------


def test_power_integrates_to_hourly_kwh_from_fixture() -> None:
    client = _client(
        _load_fixture("home_assistant_pv_production.json"),
        {Dataset.PV_PRODUCTION: "sensor.pv_power"},
    )
    raw = client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.HOUR, WINDOW)
    by_ts = dict(raw.points)

    assert raw.source == "fenecon"
    # 12:00-13:00Z sits inside a flat 3000 W plateau -> 3.0 kWh.
    assert by_ts[_ts("2024-06-15T12:00:00")] == pytest.approx(3.0)
    # 03:00-04:00Z is overnight (0 W carried from the midnight sample) -> 0.0 kWh.
    assert by_ts[_ts("2024-06-15T03:00:00")] == pytest.approx(0.0)


def test_partial_hour_is_time_weighted() -> None:
    # 1000 W for the first half hour, 3000 W for the second -> 2.0 kWh.
    payload = [
        [
            {"state": "1000", "last_changed": "2024-06-15T12:00:00+00:00"},
            {"state": "3000", "last_changed": "2024-06-15T12:30:00+00:00"},
        ]
    ]
    client = _client(payload, {Dataset.GRID_IMPORT: "sensor.import"})
    raw = client.fetch_window(Dataset.GRID_IMPORT, "home", Resolution.HOUR, WINDOW)
    assert dict(raw.points)[_ts("2024-06-15T12:00:00")] == pytest.approx(2.0)


def test_soc_is_sampled_as_a_fraction() -> None:
    payload = [[{"state": "55", "last_changed": "2024-06-15T11:30:00+00:00"}]]
    client = _client(payload, {Dataset.SOC: "sensor.soc"})
    raw = client.fetch_window(Dataset.SOC, "home", Resolution.HOUR, WINDOW)
    # End of the 11:00-12:00Z hour is 12:00Z; the 55% sample is in effect -> 0.55.
    assert dict(raw.points)[_ts("2024-06-15T11:00:00")] == pytest.approx(0.55)


def test_indoor_temperature_is_sampled_in_its_own_units_from_fixture() -> None:
    """The M10 channel that must NOT go through SoC's percent conversion.

    A temperature quietly divided by 100 would still be a smooth, plausible-looking line -- it is
    the kind of bug that survives a chart review and only surfaces when M11's optimiser starts
    reasoning about a house apparently held at a quarter of a degree.
    """
    client = _client(
        _load_fixture("home_assistant_indoor_temperature.json"),
        {Dataset.INDOOR_TEMPERATURE: "sensor.living_room_temperature"},
    )
    raw = client.fetch_window(Dataset.INDOOR_TEMPERATURE, "home", Resolution.HOUR, WINDOW)
    by_ts = dict(raw.points)

    # End of the 11:00-12:00Z hour is 12:00Z, when the 24.6 sample takes effect. Degrees, as read.
    assert by_ts[_ts("2024-06-15T11:00:00")] == pytest.approx(24.6)
    assert by_ts[_ts("2024-06-15T03:00:00")] == pytest.approx(21.4)  # overnight, carried forward


def test_ac_power_integrates_like_any_other_power_sensor() -> None:
    # AC power needs no new conversion: it is a watt sensor like the six energy flows, so it rides
    # the existing integration path. 2000 W held across the hour -> 2.0 kWh.
    payload = [
        [
            {"state": "0", "last_changed": "2024-06-15T00:00:00+00:00"},
            {"state": "2000", "last_changed": "2024-06-15T12:00:00+00:00"},
            {"state": "0", "last_changed": "2024-06-15T13:00:00+00:00"},
        ]
    ]
    client = _client(payload, {Dataset.AC_POWER: "sensor.ac_power"})
    raw = client.fetch_window(Dataset.AC_POWER, "home", Resolution.HOUR, WINDOW)
    by_ts = dict(raw.points)
    assert by_ts[_ts("2024-06-15T12:00:00")] == pytest.approx(2.0)
    assert by_ts[_ts("2024-06-15T14:00:00")] == pytest.approx(0.0)


def test_an_unmetered_air_conditioner_refuses_rather_than_estimating() -> None:
    """A house with no AC sub-meter reports nothing for the channel -- it never infers one.

    This is the real-mode half of the load-split contract. The inverter meters the house, not the
    appliance, so `household_load - something` is the tempting and wrong answer: it would fabricate
    a split from a number that never measured one, and the warehouse's identity test would then be
    checking arithmetic against itself.
    """
    client = _client({}, {Dataset.HOUSEHOLD_LOAD: "sensor.house_power"})
    with pytest.raises(HomeAssistantError, match="ENERGY_HA_ENTITY_MAP"):
        client.fetch_window(Dataset.AC_POWER, "home", Resolution.HOUR, WINDOW)


def test_unavailable_state_yields_null_not_zero() -> None:
    payload = [[{"state": "unavailable", "last_changed": "2024-06-15T00:00:00+00:00"}]]
    client = _client(payload, {Dataset.PV_PRODUCTION: "sensor.pv_power"})
    raw = client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.HOUR, WINDOW)
    assert all(value is None for _, value in raw.points)
    assert raw.null_count == raw.row_count


def test_uncovered_start_yields_null() -> None:
    # A sample that only appears mid-window leaves earlier hours uncovered -> null.
    payload = [[{"state": "2000", "last_changed": "2024-06-15T12:00:00+00:00"}]]
    client = _client(payload, {Dataset.PV_PRODUCTION: "sensor.pv_power"})
    raw = client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.HOUR, WINDOW)
    by_ts = dict(raw.points)
    assert by_ts[_ts("2024-06-15T03:00:00")] is None  # before any sample
    assert by_ts[_ts("2024-06-15T13:00:00")] == pytest.approx(2.0)  # after, carried


# -- Error paths -----------------------------------------------------------------------


def test_missing_entity_config_raises() -> None:
    client = _client([[]], {})
    with pytest.raises(HomeAssistantError, match="no Home Assistant entity"):
        client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.HOUR, WINDOW)


def test_non_hourly_resolution_raises() -> None:
    client = _client([[]], {Dataset.PV_PRODUCTION: "sensor.pv_power"})
    with pytest.raises(HomeAssistantError, match="hourly only"):
        client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.QUARTERHOUR, WINDOW)


def test_non_telemetry_dataset_raises() -> None:
    client = _client([[]], {})
    with pytest.raises(HomeAssistantError, match="not a telemetry dataset"):
        client.fetch_window(Dataset.GRID_LOAD, "home", Resolution.HOUR, WINDOW)


def test_server_error_raises_after_retries() -> None:
    client = _client(None, {Dataset.PV_PRODUCTION: "sensor.pv_power"}, status=503)
    with pytest.raises(HomeAssistantError):
        client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.HOUR, WINDOW)


def test_malformed_payload_raises() -> None:
    client = _client({"not": "a list"}, {Dataset.PV_PRODUCTION: "sensor.pv_power"})
    with pytest.raises(HomeAssistantError, match="expected a JSON array"):
        client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.HOUR, WINDOW)


def test_non_json_body_is_permanent_and_not_retried() -> None:
    # A 200 with a non-JSON body (e.g. an HTML error page) is a deterministic parse failure, so it
    # must short-circuit like the 4xx path -- not burn the retry budget as if it were transient.
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="<html>gateway error</html>")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = HomeAssistantClient(
        http,
        "http://ha.local",
        {Dataset.PV_PRODUCTION: "sensor.pv_power"},
        max_retries=3,
        sleep=sleeps.append,
    )
    with pytest.raises(HomeAssistantError, match="non-JSON body"):
        client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.HOUR, WINDOW)
    assert calls == 1  # short-circuited on the first response
    assert sleeps == []  # no backoff slept


def test_high_frequency_power_integrates_correctly_at_scale() -> None:
    # A sample every minute across the whole window (~1440 samples) at a constant 1500 W. Every
    # fully-covered hour integrates to 1.5 kWh; exercises the single-cursor path under a busy
    # sensor where the old per-boundary rescan was quadratic.
    step_ms = 60_000
    payload = [
        [
            {
                "state": "1500",
                "last_changed": datetime.fromtimestamp(t / 1000, tz=UTC).isoformat(),
            }
            for t in range(WINDOW.start_ms, WINDOW.end_ms, step_ms)
        ]
    ]
    client = _client(payload, {Dataset.GRID_IMPORT: "sensor.import"})
    raw = client.fetch_window(Dataset.GRID_IMPORT, "home", Resolution.HOUR, WINDOW)
    assert len(raw.points) == 24
    assert all(value == pytest.approx(1.5) for _, value in raw.points)


def test_reads_only_the_history_endpoint() -> None:
    # The read-only contract: the connector must only ever GET .../api/history/period/...
    seen: list[Callable[..., Any] | str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        assert request.method == "GET"
        assert "/api/history/period/" in request.url.path
        return httpx.Response(200, json=[[]])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = HomeAssistantClient(
        http, "http://ha.local", {Dataset.PV_PRODUCTION: "sensor.pv"}, max_retries=0
    )
    client.fetch_window(Dataset.PV_PRODUCTION, "home", Resolution.HOUR, WINDOW)
    assert seen == ["GET"]
