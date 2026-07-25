"""Postgres-tier tests for synthetic telemetry: idempotency, revision, and source isolation.

These prove the M1/M2 raw-zone invariants hold for the derived telemetry series too -- a rerun
is a content-hash no-op, a changed input appends a revision, and the synthetic and real sources
never collide -- plus that ``read_current_series`` (the generator's irradiance reader) returns
the latest ingestion within a window. Skips when no Postgres is available.
"""

from __future__ import annotations

from datetime import date

import pytest

from energy_platform.config import BatteryConfig, PvSystemConfig, SyntheticConfig
from energy_platform.connectors.synthetic import (
    TELEMETRY_DATASETS,
    SyntheticTelemetryClient,
    utc_hour_instants,
)
from energy_platform.connectors.types import Dataset, RawSeries, Resolution, UtcWindow
from energy_platform.orchestration.ingest import berlin_day_window, ingest_partition
from energy_platform.orchestration.raw_zone import RawZoneRepository, WriteOutcome

pytestmark = pytest.mark.postgres

PV = PvSystemConfig()
BATTERY = BatteryConfig()
SYNTHETIC = SyntheticConfig()
DAY = date(2024, 6, 15)
WINDOW = berlin_day_window(DAY)
SITE = "home"


class _SeriesStub:
    """A minimal connector that serves fixed points for one source (weather or a fake device)."""

    def __init__(self, source: str, values: dict[Dataset, dict[int, float | None]]) -> None:
        self.source = source
        self._values = values

    def fetch_window(
        self, dataset: Dataset, region: str, resolution: Resolution, window: UtcWindow
    ) -> RawSeries:
        points = tuple(
            (ts, self._values.get(dataset, {}).get(ts)) for ts in utc_hour_instants(window)
        )
        return RawSeries(
            source=self.source,
            dataset=dataset,
            region=region,
            resolution=resolution,
            source_tz="UTC",
            source_urls=(f"stub://{self.source}",),
            points=points,
        )


def _bell(peak: float) -> dict[int, float | None]:
    import math

    instants = utc_hour_instants(WINDOW)
    n = len(instants)
    return {
        ts: max(0.0, peak * math.exp(-(((i - n / 2) / (n / 5)) ** 2)))
        for i, ts in enumerate(instants)
    }


def _ingest_weather(repo: RawZoneRepository, ghi_peak: float) -> None:
    weather = _SeriesStub(
        "open_meteo",
        {
            Dataset.SHORTWAVE_RADIATION: _bell(ghi_peak),
            Dataset.TEMPERATURE_2M: dict.fromkeys(utc_hour_instants(WINDOW), 18.0),
        },
    )
    for dataset in (Dataset.SHORTWAVE_RADIATION, Dataset.TEMPERATURE_2M):
        ingest_partition(weather, repo, dataset, SITE, Resolution.HOUR, DAY)


def _ingest_telemetry(repo: RawZoneRepository) -> list[WriteOutcome]:
    client = SyntheticTelemetryClient(repo, pv=PV, battery=BATTERY, synthetic=SYNTHETIC)
    return [
        ingest_partition(client, repo, dataset, SITE, Resolution.HOUR, DAY).outcome
        for dataset in TELEMETRY_DATASETS
    ]


def test_read_current_series_returns_ingested_weather(raw_repo: RawZoneRepository) -> None:
    _ingest_weather(raw_repo, ghi_peak=800.0)
    points = raw_repo.read_current_series(
        "open_meteo",
        Dataset.SHORTWAVE_RADIATION.value,
        SITE,
        "hour",
        WINDOW.start_ms,
        WINDOW.end_ms,
    )
    assert len(points) == WINDOW.expected_count(Resolution.HOUR)
    assert max(v for _, v in points if v is not None) > 0


def test_telemetry_reingest_is_a_noop(raw_repo: RawZoneRepository) -> None:
    _ingest_weather(raw_repo, ghi_peak=800.0)
    first = _ingest_telemetry(raw_repo)
    second = _ingest_telemetry(raw_repo)
    assert all(o is WriteOutcome.LOADED for o in first)
    assert all(o is WriteOutcome.NOOP for o in second)


def test_changed_irradiance_appends_a_revision(raw_repo: RawZoneRepository) -> None:
    _ingest_weather(raw_repo, ghi_peak=800.0)
    assert all(o is WriteOutcome.LOADED for o in _ingest_telemetry(raw_repo))
    # A revised weather day changes PV, so telemetry re-ingestion is a revision, not a no-op.
    _ingest_weather(raw_repo, ghi_peak=500.0)
    outcomes = _ingest_telemetry(raw_repo)
    assert any(o is WriteOutcome.REVISION for o in outcomes)


def test_synthetic_and_fenecon_sources_coexist(raw_repo: RawZoneRepository) -> None:
    _ingest_weather(raw_repo, ghi_peak=800.0)
    _ingest_telemetry(raw_repo)
    # A real device writing the same dataset/region/day under a different source must not collide.
    fenecon = _SeriesStub(
        "fenecon", {Dataset.PV_PRODUCTION: dict.fromkeys(utc_hour_instants(WINDOW), 1.0)}
    )
    outcome = ingest_partition(
        fenecon, raw_repo, Dataset.PV_PRODUCTION, SITE, Resolution.HOUR, DAY
    ).outcome
    assert outcome is WriteOutcome.LOADED

    synthetic_pv = raw_repo.read_current_series(
        "synthetic", Dataset.PV_PRODUCTION.value, SITE, "hour", WINDOW.start_ms, WINDOW.end_ms
    )
    fenecon_pv = raw_repo.read_current_series(
        "fenecon", Dataset.PV_PRODUCTION.value, SITE, "hour", WINDOW.start_ms, WINDOW.end_ms
    )
    assert {v for _, v in fenecon_pv} == {1.0}
    assert synthetic_pv != fenecon_pv  # independent series, both retrievable


def test_stored_telemetry_conserves_energy_each_hour(raw_repo: RawZoneRepository) -> None:
    _ingest_weather(raw_repo, ghi_peak=850.0)
    _ingest_telemetry(raw_repo)
    stored = {
        dataset: dict(
            raw_repo.read_current_series(
                "synthetic", dataset.value, SITE, "hour", WINDOW.start_ms, WINDOW.end_ms
            )
        )
        for dataset in TELEMETRY_DATASETS
    }
    for ts in utc_hour_instants(WINDOW):
        maybe = {d: stored[d].get(ts) for d in TELEMETRY_DATASETS}
        if any(v is None for v in maybe.values()):
            continue
        row = {d: v for d, v in maybe.items() if v is not None}
        pv = row[Dataset.PV_PRODUCTION]
        imp = row[Dataset.GRID_IMPORT]
        dis = row[Dataset.BATTERY_DISCHARGE]
        load = row[Dataset.HOUSEHOLD_LOAD]
        exp = row[Dataset.GRID_EXPORT]
        chg = row[Dataset.BATTERY_CHARGE]
        assert (pv + imp + dis) == pytest.approx(load + exp + chg, abs=0.005)
