"""What the README figure's staleness check may forgive, and what it must still catch.

``scripts/report_regret.py --check`` runs in CI, and its first run there failed on numbers that had
not changed in any way that mattered: the fitted forecasts behind ``forecast_driven`` are only
bit-reproducible on a machine with the same thread count, library build and CPU, and under a flat
tariff the optimal schedule is not unique, so the same total splits across days differently. A check
held to the settlement quantum on all of it can only pass on the machine that generated the sidecar.

The tolerances that fix that are a claim, not a convenience -- "this number may move by this much
because of *this* known cause" -- so they are pinned here. The failure mode a loose tolerance
invites is the whole point: a genuinely stale figure that CI waves through. Every case below that
asserts a *pass* has a sibling asserting the same column still fails when it moves further.

The script lives outside ``src/`` (it imports matplotlib -- see tests/test_import_containment.py),
so it is loaded by path rather than imported.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "report_regret", ROOT / "scripts" / "report_regret.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report_regret = _load()


def _window(**overrides: float) -> dict[str, Any]:
    """One `mart_dispatch_regret` row, shaped as the sidecar stores it."""
    row: dict[str, Any] = {
        "tariff_id": "dynamic_2024",
        "region": "home",
        "naive_cost_eur": -144.759682,
        "forecast_driven_cost_eur": -141.921052,
        "perfect_foresight_cost_eur": -144.779499,
        "hindsight_cost_eur": -145.053956,
        "available_savings_eur": 0.294274,
        "regret_eur": 3.132904,
        "forecast_error_cost_eur": 2.858447,
        "myopia_cost_eur": 0.274457,
        "captured_value_share": -9.646214,
    }
    return row | overrides


def _daily(scenario: str, cumulative: float) -> dict[str, Any]:
    return {
        "tariff_id": "dynamic_2024",
        "scenario": scenario,
        "sim_day": 1,
        "local_date": "2024-05-02",
        "cumulative_net_cost_eur": cumulative,
    }


def _diff(committed: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems: list[str] = report_regret._diff(committed, current)
    return problems


def _diff_windows(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return _diff({"windows": [before], "daily": []}, {"windows": [after], "daily": []})


def _diff_daily(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return _diff({"windows": [], "daily": [before]}, {"windows": [], "daily": [after]})


# -- The exact tier: everything that is a function of the actuals alone ----------------------


@pytest.mark.parametrize(
    "column",
    ["naive_cost_eur", "hindsight_cost_eur", "perfect_foresight_cost_eur", "myopia_cost_eur"],
)
def test_an_actual_derived_figure_still_fails_on_a_hundredth_of_a_cent(column: str) -> None:
    """No solver and no fit stands behind these, so nothing entitles them to drift at all.

    A tenth of a milli-euro is below the cent the prose quotes and above the 1e-6 settlement
    quantum, which is exactly the band a rounding change would land in.
    """
    before = _window()
    after = _window(**{column: before[column] + 1e-4})
    assert _diff_windows(before, after), f"{column} must be held to the settlement quantum"


def test_the_naive_curve_is_held_exactly_too() -> None:
    """A reactive policy replayed over the actuals: nothing in it can be machine-dependent."""
    assert _diff_daily(_daily("naive_continuous", -10.0), _daily("naive_continuous", -10.001))


# -- The fit tier: forecast_driven and the two columns algebraically downstream of it --------


@pytest.mark.parametrize(
    "column", ["forecast_driven_cost_eur", "regret_eur", "forecast_error_cost_eur"]
)
def test_a_forecast_derived_figure_absorbs_a_cross_machine_refit(column: str) -> None:
    """7.6 ct is the drift measured between the machine that owns the figure and CI's runner.

    `regret_eur` and `forecast_error_cost_eur` are `forecast_driven_cost_eur` minus a quantity that
    is exact, so they move by the identical absolute amount -- which is why they share its
    tolerance rather than getting one of their own.
    """
    before = _window()
    after = _window(**{column: before[column] + 0.076039})
    assert not _diff_windows(before, after), f"{column} must survive a refit on another CPU"


@pytest.mark.parametrize(
    "column", ["forecast_driven_cost_eur", "regret_eur", "forecast_error_cost_eur"]
)
def test_a_forecast_derived_figure_still_fails_on_a_real_change(column: str) -> None:
    """The tolerance forgives a rebuild, not a stale figure. One euro is three times the band."""
    before = _window()
    after = _window(**{column: before[column] + 1.0})
    assert _diff_windows(before, after), f"{column} must not absorb a genuine change"


def test_the_captured_share_tolerance_is_derived_from_the_prize_it_divides_by() -> None:
    """A ratio over 29 cents of available savings, so the same euro drift is amplified 3.4x.

    Pinned because a share column silently sharing the euro tolerance would be the easiest way for
    this check to start failing on CI again for a reason nobody had changed.
    """
    before = _window()
    drift = 0.076039 / before["available_savings_eur"]
    assert not _diff_windows(
        before, _window(captured_value_share=before["captured_value_share"] + drift)
    )
    assert _diff_windows(before, _window(captured_value_share=before["captured_value_share"] + 5.0))


# -- The tie-break tier: a flat tariff makes the optimal schedule non-unique -----------------


@pytest.mark.parametrize("scenario", ["optimal", "perfect_foresight_plan"])
def test_a_solved_daily_curve_absorbs_a_different_tie_break(scenario: str) -> None:
    """13 ct is the largest daily divergence CI showed on the flat static tariff.

    The window total is unaffected by the tie and stays in the exact tier above -- it is only the
    split across days that the solver is free to choose differently.
    """
    assert not _diff_daily(_daily(scenario, -50.0), _daily(scenario, -50.131514))


@pytest.mark.parametrize("scenario", ["optimal", "perfect_foresight_plan"])
def test_a_solved_daily_curve_still_fails_on_a_real_change(scenario: str) -> None:
    assert _diff_daily(_daily(scenario, -50.0), _daily(scenario, -52.0))


# -- Shape, which no tolerance applies to ---------------------------------------------------


def test_a_missing_scenario_is_a_shape_change_and_no_tolerance_saves_it() -> None:
    """The failure the check most needs to catch: a scenario that stopped being written."""
    committed = {
        "windows": [],
        "daily": [_daily("optimal", -1.0), _daily("naive_continuous", -1.0)],
    }
    current = {"windows": [], "daily": [_daily("optimal", -1.0)]}
    problems = _diff(committed, current)
    assert problems and "2 committed rows, 1 in the warehouse" in problems[0]


def test_a_changed_label_is_compared_as_a_value_not_a_number() -> None:
    """Identity columns carry no tolerance path at all -- they are compared with `!=`."""
    assert _diff_daily(_daily("optimal", -1.0), _daily("forecast_driven", -1.0))
