"""Read-only telemetry connector for the real Fenecon Home 10, via Home Assistant's REST API.

This is the *real* counterpart to the synthetic generator. It is **disabled by default** and
never runs in CI: the credentials and host come only from the environment (see
:class:`~energy_platform.config.HomeAssistantConfig`), so the public repo can never reach a live
house. It is exercised offline against recorded, credential-free fixtures; the live path is
verified manually from a trusted machine over the Tailscale link.

**Read-only, by construction and by intent.** It touches only the history endpoint
(``GET /api/history/period``) -- it never writes state or calls a service. Home Assistant is one
backend; **OpenEMS Modbus TCP** is a documented alternative that would implement the same
:class:`~energy_platform.connectors.base.MarketDataConnector` protocol and land in the identical
raw-zone schema, so nothing downstream changes. If you ever add the Modbus backend, keep it
read-only too: **never point a write-capable Modbus client at a live inverter** -- a stray
coil/register write can trip or mis-configure the hardware.

Both backends produce exactly the same seven :data:`TELEMETRY_DATASETS` at hourly resolution as
the synthetic generator, differing only in ``source`` (``"fenecon"`` vs ``"synthetic"``), so
demo mode is indistinguishable in shape from real mode.

Conversion from Home Assistant state history to the hourly raw-zone grid:

* **Energy flows** (six series) are assumed to be exposed as *instantaneous power sensors in
  watts*. Each hour's energy is the time-weighted integral of that step-function power over the
  hour, in kWh. If any part of the hour is uncovered or the sensor is ``unavailable`` /
  ``unknown``, the hour is ``None`` -- surfaced, never back-filled with a guess.
* **SoC** is a *level*, not a flow, so it is sampled at the end of the hour and converted from
  percent to a fraction. An unknown level is ``None``.

Cumulative energy-counter entities (hourly = end-of-hour minus start-of-hour reading) are a
small future extension; the power-sensor model above is what M3 ships.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Final

import httpx

from energy_platform.connectors.synthetic import TELEMETRY_DATASETS, utc_hour_instants
from energy_platform.connectors.types import Dataset, RawSeries, Resolution, UtcWindow

SOURCE: Final = "fenecon"
SOURCE_TZ: Final = "UTC"

USER_AGENT: Final = "energy-platform/0.1 (+https://github.com; Home Assistant connector)"

# SoC is a level entity; the six energy flows are power (W) entities integrated to kWh.
_LEVEL_DATASETS: Final = frozenset({Dataset.SOC})
_UNKNOWN_STATES: Final = frozenset({"unavailable", "unknown", "none", ""})

_RETRYABLE_STATUS: Final = frozenset({408, 429, 500, 502, 503, 504})
_HOUR_MS: Final = 3_600_000
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class HomeAssistantError(RuntimeError):
    """Raised when Home Assistant data cannot be retrieved or parsed."""


class HomeAssistantClient:
    """Fetches one telemetry series per call from Home Assistant's history endpoint.

    The ``httpx.Client`` is injected with its ``Authorization: Bearer`` header already set by the
    caller (the resource / CLI), so the long-lived token never lives in the connector. Tests
    inject an ``httpx.MockTransport`` backed by recorded fixtures -- no live calls in CI.
    """

    source = SOURCE

    def __init__(
        self,
        http: httpx.Client,
        base_url: str,
        entity_map: Mapping[Dataset, str],
        *,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._entities = dict(entity_map)
        self._max_retries = max_retries
        self._sleep = sleep

    def fetch_window(
        self,
        dataset: Dataset,
        region: str,
        resolution: Resolution,
        window: UtcWindow,
    ) -> RawSeries:
        if resolution is not Resolution.HOUR:
            raise HomeAssistantError(
                f"Fenecon telemetry is hourly only; got resolution '{resolution.value}'"
            )
        if dataset not in TELEMETRY_DATASETS:
            raise HomeAssistantError(f"'{dataset.value}' is not a telemetry dataset")
        entity = self._entities.get(dataset)
        if entity is None:
            raise HomeAssistantError(
                f"no Home Assistant entity configured for '{dataset.value}'; "
                "set ENERGY_HA_ENTITY_MAP"
            )

        url = self._history_url(window.start_ms)
        params = self._history_params(entity, window.end_ms)
        samples = _parse_history(self._get_json(url, params), entity)

        if dataset in _LEVEL_DATASETS:
            points = tuple(
                (ts, _level_at(samples, ts + _HOUR_MS)) for ts in utc_hour_instants(window)
            )
        else:
            points = tuple(
                (ts, _integrate_power_kwh(samples, ts, ts + _HOUR_MS))
                for ts in utc_hour_instants(window)
            )

        return RawSeries(
            source=SOURCE,
            dataset=dataset,
            region=region,
            resolution=resolution,
            source_tz=SOURCE_TZ,
            source_urls=(str(httpx.URL(url, params=params)),),
            points=points,
        )

    def _history_url(self, start_ms: int) -> str:
        start = _iso(start_ms)
        return f"{self._base_url}/api/history/period/{start}"

    def _history_params(self, entity: str, end_ms: int) -> dict[str, Any]:
        return {
            "filter_entity_id": entity,
            "end_time": _iso(end_ms),
            # Trim attribute payloads we do not use; states + timestamps are all we read.
            "minimal_response": "true",
        }

    def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.get(url, params=params)
                if response.status_code in _RETRYABLE_STATUS:
                    raise HomeAssistantError(f"Home Assistant returned {response.status_code}")
                response.raise_for_status()
                try:
                    return response.json()
                except json.JSONDecodeError as exc:
                    raise HomeAssistantError("Home Assistant returned a non-JSON body") from exc
            except (httpx.TransportError, HomeAssistantError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    self._sleep(0.5 * (2**attempt))
            except httpx.HTTPStatusError as exc:  # 4xx (e.g. 401 bad token) -- permanent
                raise HomeAssistantError(f"Home Assistant request failed: {url}") from exc
        raise HomeAssistantError(f"Home Assistant failed after retries: {url}") from last_exc


# A parsed state sample: (epoch ms, numeric value) or (epoch ms, None) for unavailable/unknown.
_Sample = tuple[int, float | None]


def _parse_history(payload: Any, entity: str) -> list[_Sample]:
    """Flatten HA's ``[[state, ...]]`` history for one entity into sorted numeric samples."""
    if not isinstance(payload, list):
        raise HomeAssistantError("malformed history: expected a JSON array")
    if not payload:
        return []
    series = payload[0]
    if not isinstance(series, list):
        raise HomeAssistantError("malformed history: expected a per-entity array")
    samples: list[_Sample] = []
    for entry in series:
        if not isinstance(entry, dict):
            raise HomeAssistantError(f"malformed history entry for '{entity}': {entry!r}")
        ts = entry.get("last_changed") or entry.get("last_updated")
        if ts is None:
            continue
        samples.append((_parse_iso_ms(str(ts)), _parse_state(entry.get("state"))))
    samples.sort(key=lambda s: s[0])
    return samples


def _integrate_power_kwh(samples: list[_Sample], h0: int, h1: int) -> float | None:
    """Time-weighted integral of a step-function power (W) over ``[h0, h1)`` ms, in kWh.

    Returns ``None`` if any sub-interval of the hour has no known numeric value -- the state
    before it is unavailable/unknown, or nothing is known at ``h0`` (uncovered start).
    """
    boundaries = [h0, *(ts for ts, _ in samples if h0 < ts < h1), h1]
    wh = 0.0
    for a, b in pairwise(boundaries):
        if b <= a:
            continue
        active = _active_value(samples, a)
        if active is None:
            return None
        wh += active * (b - a) / _HOUR_MS  # W * hours -> Wh
    return wh / 1000.0


def _level_at(samples: list[_Sample], instant: int) -> float | None:
    """The level (percent) in effect at ``instant``, as a fraction; ``None`` if unknown."""
    value = _active_value(samples, instant)
    return None if value is None else value / 100.0


def _active_value(samples: list[_Sample], instant: int) -> float | None:
    """Value of the most recent sample at or before ``instant`` (``None`` if none / unavailable)."""
    active: float | None = None
    for ts, value in samples:
        if ts > instant:
            break
        active = value
    return active


def _parse_state(state: object) -> float | None:
    if not isinstance(state, str) or state.strip().lower() in _UNKNOWN_STATES:
        return None
    try:
        return float(state)
    except ValueError:
        return None


def _parse_iso_ms(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _iso(ts_ms: int) -> str:
    return (_EPOCH + timedelta(milliseconds=ts_ms)).isoformat()
