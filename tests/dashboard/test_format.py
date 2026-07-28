"""Unit tests for the display layer. No database, no Streamlit runtime, hand-built payloads.

Modelled on ``tests/test_report_regret_check.py``, which tests a reporting tool's logic the same
way. What is worth pinning here is not that a float becomes a string -- it is the two conventions
the display layer is responsible for, both of which are places a well-meaning UI would normally
get it wrong: shares are never clamped, and missing never becomes zero.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from dashboard import format as fmt


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "€0.00"), (1234.5, "€1,234.50"), (-12.345, "€-12.35"), (None, fmt.MISSING)],
)
def test_euro(value: float | None, expected: str) -> None:
    assert fmt.euro(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, fmt.MISSING), (0.0, "0.0 kWh"), (12.34, "12.3 kWh")],
)
def test_kwh(value: float | None, expected: str) -> None:
    assert fmt.kwh(value) == expected


def test_a_share_below_zero_keeps_its_sign() -> None:
    """M8's headline is -965%. Clamping it to zero would delete the finding."""
    assert fmt.share_percent(-9.65) == "-965.0%"


def test_a_share_above_one_is_not_clipped_either() -> None:
    """Above 1 is a bug in the warehouse, and it must be visible rather than silently plausible."""
    assert fmt.share_percent(1.5) == "150.0%"


def test_missing_never_becomes_zero() -> None:
    """A gap is not a measurement of nothing; it is the absence of a measurement."""
    assert fmt.euro(None) == fmt.MISSING
    assert fmt.kwh(None) == fmt.MISSING
    assert fmt.share_percent(None) == fmt.MISSING
    assert fmt.hours(None) == fmt.MISSING
    assert fmt.freshness(None) == fmt.MISSING


def test_completeness_note_is_silent_on_a_complete_period() -> None:
    row = {"is_partial_month": False, "completeness_ratio": 1.0}
    assert fmt.completeness_note(row) is None


def test_completeness_note_speaks_on_a_partial_month() -> None:
    row = {
        "is_partial_month": True,
        "completeness_ratio": 0.1279,
        "priced_hours": 95,
        "expected_hours": 743,
    }
    note = fmt.completeness_note(row)
    assert note is not None
    assert "95" in note
    assert "743" in note


def test_completeness_note_accepts_the_window_vocabulary_too() -> None:
    """The window-grain marts flag is_partial_window; the same sentence has to cover both."""
    row = {
        "is_partial_window": True,
        "completeness_ratio": 0.5,
        "priced_hours": 84,
        "expected_hours": 168,
    }
    note = fmt.completeness_note(row)
    assert note is not None
    assert "84" in note


def test_completeness_note_falls_back_when_counts_are_absent() -> None:
    note = fmt.completeness_note({"is_partial_month": True, "completeness_ratio": None})
    assert note == "Partial period — partly covered."


def test_freshness_switches_units_where_hours_stop_being_readable() -> None:
    assert fmt.freshness(timedelta(hours=6)) == "6 h ago"
    assert fmt.freshness(timedelta(days=30)) == "30 d ago"


def test_scenario_labels_come_from_the_shared_palette() -> None:
    """The figure and the dashboard must call the same scenario the same thing."""
    from energy_platform.palette import SCENARIO_LABELS

    assert fmt.scenario_label("forecast_driven") == SCENARIO_LABELS["forecast_driven"]


def test_an_unknown_scenario_still_gets_a_readable_label() -> None:
    assert fmt.scenario_label("no_battery") == "No battery"
