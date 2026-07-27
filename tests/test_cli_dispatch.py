"""Guard: an ad-hoc dispatch window is solved and reported, and never written.

The derived zone holds the declared coverage windows and nothing else. That is not tidiness -- it
is what lets ``assert_dispatch_windows_are_declared`` treat every other window as stale output,
without a flag column to dodge or an exception to carve out. It is also not self-enforcing: the
replace-write deletes only the key it is about to write, so a single ad-hoc row would fail that test
on every subsequent build, permanently, with no command in the repo that removes it. One exploratory
``just dispatch --from ... --to ...`` used to poison the warehouse until someone hand-deleted rows.

These tests pin the write path rather than the solve: the optimiser's own behaviour is covered by
tests/dispatch/, and what matters here is which calls reach the repository.

No Postgres. The repository is stubbed, so the connection is a formality and the assertions are
about the calls the CLI chooses to make.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from energy_platform import cli
from energy_platform.config import AppConfig
from energy_platform.dispatch.model import HourInputs
from energy_platform.dispatch.store import WriteCounts
from energy_platform.dispatch.windows import CoverageWindow
from tests.dispatch.conftest import make_hours

REGION = "home"

# A day of cheap night and expensive evening, so the optimum is a real dispatch rather than an idle
# battery -- an ad-hoc run that solved nothing would pass the "did not persist" assertions by
# doing nothing at all.
PRICES = [20.0] * 6 + [200.0] * 6 + [20.0] * 6 + [300.0] * 6
HOURS = make_hours(PRICES, pv_kwh=[0.0] * 24, load_kwh=[1.0] * 24)


class _StubRepository:
    """Records what the CLI asked of it. Every method the dispatch path touches is here."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.calls: list[str] = []

    def ensure_schema(self) -> None:
        self.calls.append("ensure_schema")

    def input_relation_exists(self) -> bool:
        return True

    def regions(self, _window: CoverageWindow) -> tuple[str, ...]:
        return (REGION,)

    def read_window(self, _window: CoverageWindow, _region: str) -> tuple[HourInputs, ...]:
        return HOURS

    def replace_window(self, *_args: object, **_kwargs: object) -> WriteCounts:
        self.calls.append("replace_window")
        return WriteCounts(runs=4, hours=len(HOURS), replaced=0)


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> _StubRepository:
    """Replace the repository and the connection; leave the solver and the tariffs real."""
    stub = _StubRepository()

    @contextmanager
    def _connect(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(psycopg, "connect", _connect)
    monkeypatch.setattr(cli, "DispatchRepository", lambda *a, **k: stub)
    return stub


# -- Ad-hoc windows are not persisted ----------------------------------------------------------


def test_an_ad_hoc_window_is_solved_but_never_written(repo: _StubRepository) -> None:
    """The finding itself: --from/--to used to write rows no later command could remove."""
    exit_code = cli.main(["dispatch", "--from", "2024-06-10", "--to", "2024-06-12"])

    assert exit_code == 0
    assert "replace_window" not in repo.calls, (
        "an ad-hoc window was persisted; assert_dispatch_windows_are_declared will report it as "
        "stale on every subsequent build, and nothing deletes it"
    )


def test_an_ad_hoc_window_does_not_create_the_derived_zone(repo: _StubRepository) -> None:
    """Exploring must not leave a schema behind: there is nothing for it to hold."""
    cli.main(["dispatch", "--from", "2024-06-10", "--to", "2024-06-12"])

    assert "ensure_schema" not in repo.calls


def test_the_declared_windows_are_still_persisted(repo: _StubRepository) -> None:
    """The other half of the claim -- without this, "does not write" passes by writing nothing."""
    exit_code = cli.main(["dispatch"])

    assert exit_code == 0
    args = _ns()
    specs, _ = cli._dispatch_tariffs(args, AppConfig.from_env())
    assert "ensure_schema" in repo.calls
    assert repo.calls.count("replace_window") == len(cli._dispatch_windows(args)) * len(specs), (
        "expected one replace-write per (declared window, site, consumption tariff)"
    )


def _ns() -> Any:
    """The namespace `dispatch` with no arguments produces, for counting declared windows."""
    return cli._build_parser().parse_args(["dispatch"])


# -- --output, the scratch escape hatch --------------------------------------------------------


def test_output_writes_every_solve_including_the_unpersisted_ones(
    repo: _StubRepository, tmp_path: Path
) -> None:
    """The reason ad-hoc runs need no warehouse row: the result can be kept as a file instead."""
    out = tmp_path / "scratch.json"
    cli.main(["dispatch", "--from", "2024-06-10", "--to", "2024-06-12", "--output", str(out)])

    payload = json.loads(out.read_text())
    assert payload["persisted"] is False
    assert len(payload["solves"]) == 2, (
        "one entry per (window, site, tariff); two tariffs, one site"
    )

    solve = payload["solves"][0]
    assert solve["window_start"] == "2024-06-10"
    assert solve["window_end"] == "2024-06-12"
    assert solve["region"] == REGION
    scenarios = {s["scenario"] for s in solve["scenarios"]}
    assert scenarios == {"no_battery", "naive_telemetered", "naive_continuous", "optimal"}
    assert all(len(s["hours"]) == len(HOURS) for s in solve["scenarios"])


def test_the_output_carries_the_three_factors_of_the_terminal_credit(
    repo: _StubRepository, tmp_path: Path
) -> None:
    """The mart's reconstruction contract, so the scratch file is not a lesser record."""
    out = tmp_path / "scratch.json"
    cli.main(["dispatch", "--from", "2024-06-10", "--to", "2024-06-12", "--output", str(out)])

    payload = json.loads(out.read_text())
    for solve in payload["solves"]:
        rate_eur_kwh = solve["terminal_value_ct_kwh"] / 100
        for scenario in solve["scenarios"]:
            recomputed = (
                rate_eur_kwh
                * scenario["terminal_discharge_efficiency"]
                * scenario["terminal_soc_delta_kwh"]
            )
            assert scenario["terminal_value_eur"] == pytest.approx(recomputed, abs=5e-7)
            assert scenario["adjusted_net_cost_eur"] == pytest.approx(
                scenario["net_cost_eur"] - scenario["terminal_value_eur"], abs=5e-7
            )


def test_output_marks_a_declared_run_as_persisted(repo: _StubRepository, tmp_path: Path) -> None:
    """The flag is the file's own account of whether the warehouse also holds this."""
    out = tmp_path / "scratch.json"
    cli.main(["dispatch", "--output", str(out)])

    assert json.loads(out.read_text())["persisted"] is True


# -- Argument handling -------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--from", "2024-06-10"], ["--to", "2024-06-12"]])
def test_from_and_to_must_be_given_together(argv: Sequence[str]) -> None:
    with pytest.raises(SystemExit, match="must be given together"):
        cli.main(["dispatch", *argv])


def test_a_reversed_window_is_refused() -> None:
    with pytest.raises(SystemExit, match="ends before it starts"):
        cli.main(["dispatch", "--from", "2024-06-12", "--to", "2024-06-10"])


def test_the_declared_windows_are_the_default() -> None:
    """Read from dbt_project.yml, the same file the hourly spine is built from."""
    windows = cli._dispatch_windows(_ns())

    assert windows
    assert all(isinstance(window, CoverageWindow) for window in windows)


def test_an_ad_hoc_window_is_exactly_what_was_asked_for() -> None:
    args = cli._build_parser().parse_args(
        ["dispatch", "--from", "2024-06-10", "--to", "2024-06-12"]
    )

    assert cli._dispatch_windows(args) == (
        CoverageWindow(start=date(2024, 6, 10), end=date(2024, 6, 12)),
    )
