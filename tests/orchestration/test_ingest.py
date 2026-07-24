"""Pure-logic tests for the DST-aware window builder and content hashing."""

from __future__ import annotations

from datetime import date

import pytest

from energy_platform.connectors.types import Dataset, Point, Resolution
from energy_platform.orchestration.ingest import berlin_day_window, content_hash

NORMAL_DAY = date(2024, 6, 12)
MARCH_DST_DAY = date(2024, 3, 31)
OCTOBER_DST_DAY = date(2024, 10, 27)


@pytest.mark.parametrize(
    ("day", "hours"),
    [
        (NORMAL_DAY, 24),
        (MARCH_DST_DAY, 23),  # spring-forward: 02:00 local hour does not exist
        (OCTOBER_DST_DAY, 25),  # fall-back: 02:00-03:00 local hour occurs twice
    ],
)
def test_berlin_day_window_spans_dst_correctly(day: date, hours: int) -> None:
    window = berlin_day_window(day)
    assert window.hours == hours
    assert window.expected_count(Resolution.HOUR) == hours
    assert window.expected_count(Resolution.QUARTERHOUR) == hours * 4


def test_windows_are_contiguous_across_a_dst_boundary() -> None:
    """One day's window end equals the next day's start -- no gap or overlap at the switch."""
    assert berlin_day_window(MARCH_DST_DAY).end_ms == berlin_day_window(date(2024, 4, 1)).start_ms


_POINTS: tuple[Point, ...] = ((1_000, 129.73), (4_600, None), (8_200, 120.57))


def test_content_hash_is_deterministic() -> None:
    a = content_hash(Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, NORMAL_DAY, _POINTS)
    b = content_hash(Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, NORMAL_DAY, _POINTS)
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_content_hash_changes_when_a_value_changes() -> None:
    changed: tuple[Point, ...] = ((1_000, 129.74), (4_600, None), (8_200, 120.57))
    args = (Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, NORMAL_DAY)
    assert content_hash(*args, changed) != content_hash(*args, _POINTS)


def test_content_hash_distinguishes_null_from_zero() -> None:
    """A missing value must not hash the same as a real 0.0 -- NaN over fabrication."""
    with_null: tuple[Point, ...] = ((1_000, None),)
    with_zero: tuple[Point, ...] = ((1_000, 0.0),)
    args = (Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, NORMAL_DAY)
    assert content_hash(*args, with_null) != content_hash(*args, with_zero)


def test_content_hash_varies_by_dataset_and_partition() -> None:
    price = content_hash(Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, NORMAL_DAY, _POINTS)
    load = content_hash(Dataset.GRID_LOAD, "DE", Resolution.HOUR, NORMAL_DAY, _POINTS)
    other_day = content_hash(
        Dataset.DAY_AHEAD_PRICE, "DE", Resolution.HOUR, OCTOBER_DST_DAY, _POINTS
    )
    assert price != load
    assert price != other_day
