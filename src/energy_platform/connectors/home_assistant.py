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
* **Indoor temperature** (M10) is also sampled at the end of the hour, but in the sensor's own
  degrees Celsius -- no scaling. It gets its own branch rather than joining SoC's because a
  temperature silently divided by 100 would still plot as a smooth, plausible line.

Cumulative energy-counter entities (hourly = end-of-hour minus start-of-hour reading) are a
small future extension; the power-sensor model above is what M3 ships.

**Real-mode AC power needs its own meter, and where there is none the channel is null.** M10 adds
``load_base`` and ``ac_power`` as separable components of ``household_load``, but nothing in a
Fenecon reading can separate them: the inverter meters the house, not the appliances. A real
deployment must point ``ac_power`` at a genuinely separate source -- the Samsung/HVAC integration's
own power sensor, or an external plug/circuit meter -- and map it through
``ENERGY_HA_ENTITY_MAP``. Where no such entity is configured, ``ac_power`` and ``load_base`` are
simply **not ingested**: they are never estimated, never derived by subtraction, and never assumed
to be zero. ``household_load`` continues to stand alone as the canonical total, and the warehouse's
load-split identity is scoped to hours where all three are present precisely so that this house
remains a first-class citizen rather than a test failure.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx

from energy_platform.connectors.synthetic import TELEMETRY_DATASETS, utc_hour_instants
from energy_platform.connectors.types import Dataset, RawSeries, Resolution, UtcWindow

SOURCE: Final = "fenecon"
SOURCE_TZ: Final = "UTC"

USER_AGENT: Final = "energy-platform/0.1 (+https://github.com; Home Assistant connector)"

# Three conversions, because "is it a level or a flow?" is not the only question -- a level still
# has to be in the right units. SoC arrives as a percentage and is divided by 100; indoor
# temperature arrives in degrees and must be sampled *without* being scaled, which is why it cannot
# simply join _PERCENT_LEVEL_DATASETS. Everything else is an instantaneous power sensor in watts,
# integrated over the hour to kWh.
_PERCENT_LEVEL_DATASETS: Final = frozenset({Dataset.SOC})
_SAMPLED_DATASETS: Final = frozenset({Dataset.INDOOR_TEMPERATURE})
_UNKNOWN_STATES: Final = frozenset({"unavailable", "unknown", "none", ""})

_RETRYABLE_STATUS: Final = frozenset({408, 429, 500, 502, 503, 504})
_HOUR_MS: Final = 3_600_000
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class HomeAssistantError(RuntimeError):
    """Raised when Home Assistant data cannot be retrieved or parsed."""


class _RetryableError(HomeAssistantError):
    """Internal marker for transient failures (retryable status codes) worth another attempt.

    Parse failures and 4xx responses are *not* this -- they are permanent and short-circuit the
    retry loop -- so only transport errors and this are retried.
    """


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

        url, params = self.build_history_request(entity, window)
        samples = _parse_history(self._get_json(url, params), entity)

        if dataset in _PERCENT_LEVEL_DATASETS:
            points = tuple(
                (ts, _percent_level_at(samples, ts + _HOUR_MS)) for ts in utc_hour_instants(window)
            )
        elif dataset in _SAMPLED_DATASETS:
            points = tuple(
                (ts, _sampled_at(samples, ts + _HOUR_MS)) for ts in utc_hour_instants(window)
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

    def build_history_request(self, entity: str, window: UtcWindow) -> tuple[str, dict[str, Any]]:
        """The exact ``(url, params)`` this client GETs for ``entity`` over ``window``.

        Public so the fixture recorder can replay the identical request without reaching into
        private helpers -- keeping the recorded shape from drifting from production.
        ``fetch_window`` routes through here too, so there is one source of truth for the request.
        """
        return self._history_url(window.start_ms), self._history_params(entity, window.end_ms)

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
                    raise _RetryableError(f"Home Assistant returned {response.status_code}")
                response.raise_for_status()
            except (httpx.TransportError, _RetryableError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    self._sleep(0.5 * (2**attempt))
                continue
            except httpx.HTTPStatusError as exc:  # 4xx (e.g. 401 bad token) -- permanent
                raise HomeAssistantError(f"Home Assistant request failed: {url}") from exc
            # Success path: a parse failure is deterministic, so short-circuit like the 4xx path
            # rather than retrying a body that will never become valid JSON.
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise HomeAssistantError(f"Home Assistant returned a non-JSON body: {url}") from exc
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

    Walks the pre-sorted samples with a single forward cursor (no per-boundary rescan), so a day's
    conversion is linear in the sample count rather than quadratic.
    """
    n = len(samples)
    # Active value at h0: the most recent sample at or before the hour's start.
    idx = 0
    active: float | None = None
    while idx < n and samples[idx][0] <= h0:
        active = samples[idx][1]
        idx += 1

    wh = 0.0
    cursor = h0
    while cursor < h1:
        # The next boundary is the next sample inside the hour, else the hour's end.
        nxt = samples[idx][0] if idx < n and samples[idx][0] < h1 else h1
        if active is None:
            return None
        wh += active * (nxt - cursor) / _HOUR_MS  # W * hours -> Wh
        cursor = nxt
        # Adopt every sample landing at the new cursor (<= handles duplicate timestamps).
        while idx < n and samples[idx][0] <= cursor:
            active = samples[idx][1]
            idx += 1
    return wh / 1000.0


def _percent_level_at(samples: list[_Sample], instant: int) -> float | None:
    """The level (percent) in effect at ``instant``, as a fraction; ``None`` if unknown."""
    value = _active_value(samples, instant)
    return None if value is None else value / 100.0


def _sampled_at(samples: list[_Sample], instant: int) -> float | None:
    """The state in effect at ``instant``, in the sensor's own units; ``None`` if unknown.

    Used for indoor temperature, whose degrees Celsius are already the unit the raw zone wants --
    the point of a separate function from :func:`_percent_level_at` is that a temperature quietly
    divided by 100 would still look plausible in a chart.
    """
    return _active_value(samples, instant)


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
