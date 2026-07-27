"""The declared coverage windows: one file, two readers.

The hourly spine is generated from ``coverage_windows`` in ``dbt/dbt_project.yml`` -- declared, not
inferred from the data -- so that the two disjoint seeded weeks never manifest a seven-month void
between them. The optimiser solves over exactly those windows, and reads them from **the same
file** rather than keeping its own copy, for the same reason
:mod:`energy_platform.tariffs.catalog` reads ``dbt/seeds/tariffs.csv`` directly: a second copy of a
declaration is a staleness bug waiting to happen, and rendering one from the other only trades it
for a "re-render and diff" check in CI.

Window bounds are Europe/Berlin calendar dates, **inclusive at both ends**, matching the dbt var.
Converting them to instants reuses :func:`~energy_platform.orchestration.ingest.berlin_day_window`,
so a window containing a DST transition spans 23 or 25 hours on that day and the optimiser's hour
count agrees with the spine's by construction rather than by coincidence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import yaml

from energy_platform.connectors.types import Resolution, UtcWindow
from energy_platform.orchestration.ingest import berlin_day_window

# ``.../src/energy_platform/dispatch/windows.py`` -> the repo root is four parents up, the same
# arithmetic tariffs/catalog.py uses to find the seed CSV.
DEFAULT_DBT_PROJECT_PATH: Final = Path(__file__).resolve().parents[3] / "dbt" / "dbt_project.yml"
DBT_PROJECT_PATH_ENV: Final = "ENERGY_DBT_PROJECT"

_VAR_NAME: Final = "coverage_windows"


@dataclass(frozen=True, slots=True)
class CoverageWindow:
    """One declared window of Europe/Berlin calendar days, inclusive at both ends."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"coverage window ends before it starts: {self.start}..{self.end}")

    @property
    def utc_window(self) -> UtcWindow:
        """The half-open UTC instant window ``[start_ms, end_ms)`` the days span."""
        return UtcWindow(
            start_ms=berlin_day_window(self.start).start_ms,
            end_ms=berlin_day_window(self.end).end_ms,
        )

    @property
    def expected_hours(self) -> int:
        """Hours the window claims, from the Berlin calendar -- never counted from the rows.

        A window containing the spring-forward day is an hour shorter than its day count suggests,
        and one containing fall-back an hour longer. Deriving this from the calendar rather than
        from the data is what makes a truncated window fail instead of passing by silence, exactly
        as ``berlin_month_hours`` does for the monthly marts.
        """
        return self.utc_window.expected_count(Resolution.HOUR)

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


def dbt_project_path() -> Path:
    """The dbt project file to read windows from: ``ENERGY_DBT_PROJECT`` if set, else the repo's."""
    override = os.environ.get(DBT_PROJECT_PATH_ENV)
    return Path(override) if override else DEFAULT_DBT_PROJECT_PATH


def load_coverage_windows(path: Path | None = None) -> tuple[CoverageWindow, ...]:
    """Load the declared coverage windows, in the order the dbt project declares them.

    Raises :class:`FileNotFoundError` when the project file is absent and :class:`ValueError`
    naming the offending entry when the var is missing or malformed -- the same "fail with a name"
    posture as :func:`energy_platform.tariffs.catalog.load_catalog`.
    """
    source = path or dbt_project_path()
    if not source.exists():
        raise FileNotFoundError(
            f"dbt project not found at {source}; set {DBT_PROJECT_PATH_ENV} to point at it"
        )

    project: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(project, dict):
        raise ValueError(f"{source} is not a YAML mapping")

    variables = project.get("vars")
    if not isinstance(variables, dict) or _VAR_NAME not in variables:
        raise ValueError(f"{source} declares no vars.{_VAR_NAME}")

    declared = variables[_VAR_NAME]
    if not isinstance(declared, list) or not declared:
        raise ValueError(f"{source}: vars.{_VAR_NAME} must be a non-empty list of windows")

    return tuple(_window_from_entry(entry, source, index) for index, entry in enumerate(declared))


def _window_from_entry(entry: object, source: Path, index: int) -> CoverageWindow:
    where = f"{source}: vars.{_VAR_NAME}[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(f"{where} is not a mapping with 'start' and 'end'")
    try:
        start, end = entry["start"], entry["end"]
    except KeyError as exc:
        raise ValueError(f"{where} is missing {exc.args[0]!r}") from exc
    try:
        return CoverageWindow(start=_as_date(start), end=_as_date(end))
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from exc


def _as_date(value: object) -> date:
    """Accept both quoted and unquoted YAML dates -- PyYAML parses the latter into ``date``.

    ``datetime`` is narrowed to its date part first: it is a subclass of ``date``, so letting one
    through untouched would put a value with a time component into a field the rest of the module
    treats as a calendar day.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"expected a YYYY-MM-DD date, got {value!r}")
