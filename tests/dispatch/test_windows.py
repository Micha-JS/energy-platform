"""The coverage-window reader: one file, two readers, and the DST arithmetic that follows.

The optimiser solves exactly the windows ``dbt/dbt_project.yml`` declares, read from that file
rather than from a copy. These tests pin both halves of that: the parsing, and the hour counts the
Berlin calendar implies -- which are what a truncated warehouse is checked against.

Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from energy_platform.dispatch.windows import (
    DEFAULT_DBT_PROJECT_PATH,
    CoverageWindow,
    dbt_project_path,
    load_coverage_windows,
)


def test_the_shipped_project_declares_the_two_seeded_dst_windows() -> None:
    """Reads the real file: if the var moves, this fails rather than the optimiser idling."""
    windows = load_coverage_windows()

    assert windows == (
        CoverageWindow(start=date(2024, 3, 28), end=date(2024, 4, 3)),
        CoverageWindow(start=date(2024, 10, 24), end=date(2024, 10, 30)),
    )


def test_the_windows_are_23_and_25_hour_weeks_not_168_hour_ones() -> None:
    """The whole reason the hour count is derived from the calendar and never from the rows.

    The March window contains the spring-forward Sunday and is 167 hours; the October window
    contains fall-back and is 169. A naive ``7 * 24`` would be wrong in both directions, and a count
    taken from the data would shrink in lockstep with a truncated mart and pass by silence.
    """
    march, october = load_coverage_windows()

    assert march.expected_hours == 167
    assert october.expected_hours == 169


def test_a_window_covering_no_transition_is_a_plain_multiple_of_24() -> None:
    """The control case, so the numbers above are about DST rather than about off-by-ones."""
    assert CoverageWindow(start=date(2024, 6, 10), end=date(2024, 6, 16)).expected_hours == 168
    assert CoverageWindow(start=date(2024, 6, 10), end=date(2024, 6, 10)).expected_hours == 24


def test_a_window_is_inclusive_at_both_ends_matching_the_dbt_var() -> None:
    """The spine treats `end` as a day it covers, so a window of one day is 24 hours, not zero."""
    single = CoverageWindow(start=date(2024, 6, 10), end=date(2024, 6, 10))
    utc = single.utc_window

    assert utc.hours == 24
    assert str(single) == "2024-06-10..2024-06-10"


def test_a_backwards_window_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="ends before it starts"):
        CoverageWindow(start=date(2024, 6, 16), end=date(2024, 6, 10))


# -- Failure modes name what is wrong ---------------------------------------------------------


def test_a_missing_project_file_says_which_variable_points_at_it(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="ENERGY_DBT_PROJECT"):
        load_coverage_windows(tmp_path / "absent.yml")


def test_a_project_without_the_var_fails_rather_than_solving_nothing(tmp_path: Path) -> None:
    """Silently solving zero windows would look exactly like a successful run that found nothing."""
    project = tmp_path / "dbt_project.yml"
    project.write_text("name: energy_platform\nvars:\n  price_region: DE\n", encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape("declares no vars.coverage_windows")):
        load_coverage_windows(project)


def test_an_empty_window_list_fails_rather_than_solving_nothing(tmp_path: Path) -> None:
    project = tmp_path / "dbt_project.yml"
    project.write_text("vars:\n  coverage_windows: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty list"):
        load_coverage_windows(project)


def test_a_malformed_entry_is_named_by_index(tmp_path: Path) -> None:
    """Fail with a name, the same posture load_catalog takes on a bad tariff row."""
    project = tmp_path / "dbt_project.yml"
    project.write_text(
        'vars:\n  coverage_windows:\n    - {start: "2024-01-01", end: "2024-01-07"}\n'
        '    - {start: "2024-02-01"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=re.escape("coverage_windows[1] is missing 'end'")):
        load_coverage_windows(project)


def test_unquoted_yaml_dates_are_accepted_as_well_as_quoted_ones(tmp_path: Path) -> None:
    """PyYAML parses a bare 2024-01-01 into a date; both spellings must mean the same thing."""
    project = tmp_path / "dbt_project.yml"
    project.write_text(
        "vars:\n  coverage_windows:\n    - {start: 2024-01-01, end: 2024-01-07}\n", encoding="utf-8"
    )

    assert load_coverage_windows(project) == (
        CoverageWindow(start=date(2024, 1, 1), end=date(2024, 1, 7)),
    )


def test_the_project_path_is_overridable_for_a_relocated_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert dbt_project_path() == DEFAULT_DBT_PROJECT_PATH
    monkeypatch.setenv("ENERGY_DBT_PROJECT", str(tmp_path / "elsewhere.yml"))
    assert dbt_project_path() == tmp_path / "elsewhere.yml"
