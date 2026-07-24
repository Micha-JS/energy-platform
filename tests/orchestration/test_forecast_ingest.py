"""Pure-logic tests for forecast content hashing (the vintage idempotency signal)."""

from __future__ import annotations

from datetime import date

from energy_platform.connectors.types import Dataset, Point, Resolution
from energy_platform.orchestration.ingest import forecast_content_hash

ISSUE = date(2026, 7, 24)
SERIES: dict[Dataset, tuple[Point, ...]] = {
    Dataset.SHORTWAVE_RADIATION: ((0, 0.0), (3_600_000, 100.0)),
    Dataset.TEMPERATURE_2M: ((0, 12.0), (3_600_000, 13.0)),
}


def _hash(
    series: dict[Dataset, tuple[Point, ...]], *, region: str = "home", issue: date = ISSUE
) -> str:
    return forecast_content_hash("open_meteo", region, Resolution.HOUR, issue, series)


def test_forecast_hash_is_deterministic() -> None:
    assert _hash(SERIES) == _hash(SERIES)
    assert len(_hash(SERIES)) == 64  # sha256 hex


def test_forecast_hash_ignores_variable_ordering() -> None:
    """Variables are sorted before hashing, so dict insertion order never affects the digest."""
    reordered: dict[Dataset, tuple[Point, ...]] = {
        Dataset.TEMPERATURE_2M: SERIES[Dataset.TEMPERATURE_2M],
        Dataset.SHORTWAVE_RADIATION: SERIES[Dataset.SHORTWAVE_RADIATION],
    }
    assert _hash(SERIES) == _hash(reordered)


def test_forecast_hash_changes_when_a_value_changes() -> None:
    changed = {**SERIES, Dataset.TEMPERATURE_2M: ((0, 12.0), (3_600_000, 13.5))}
    assert _hash(changed) != _hash(SERIES)


def test_forecast_hash_distinguishes_null_from_zero() -> None:
    """A missing forecast value must not hash the same as a real 0.0 -- NaN over fabrication."""
    with_null: dict[Dataset, tuple[Point, ...]] = {Dataset.CLOUD_COVER: ((0, None),)}
    with_zero: dict[Dataset, tuple[Point, ...]] = {Dataset.CLOUD_COVER: ((0, 0.0),)}
    assert _hash(with_null) != _hash(with_zero)


def test_forecast_hash_varies_by_issue_date_and_region() -> None:
    base = _hash(SERIES)
    assert base != _hash(SERIES, issue=date(2026, 7, 25))
    assert base != _hash(SERIES, region="office")
