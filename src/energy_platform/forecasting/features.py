"""Feature construction, and the check that makes a leaked feature impossible to ship.

Every feature carries the instant its value became knowable. Building a matrix asserts that no
cell's ``available_at_ms`` is later than the issue time of the prediction it feeds, and raises
:class:`LookaheadError` naming the offender if one is. That is the whole mechanism -- it is small
on purpose, because a backtesting harness nobody can read is a backtesting harness nobody checks.

Three kinds of feature, and the availability of each is a claim that can be wrong:

* **Calendar and solar geometry** -- knowable arbitrarily far ahead (``available_at_ms=None``).
  Which hour of which weekday a timestamp is, and where the sun will be, are facts about the
  calendar and about orbital mechanics; neither waits for data. This is the one kind that is safe
  by construction rather than by check.
* **Vintage** -- knowable at the vintage's ``issue_time``. Guaranteed correct by
  :func:`~energy_platform.forecasting.vintage.select_vintage`, and checked anyway. "Guaranteed by
  construction" is precisely the kind of claim that stops being true when someone adds a second
  call site.
* **Lagged actual** -- knowable once the hour it summarises has closed and the reporting lag has
  elapsed, which is :func:`~energy_platform.forecasting.vintage.observable_at` and not a local
  ``ts + lag``. This is the one that catches real mistakes: on this platform the lag is five days,
  so the obvious "yesterday's value" feature is lookahead and gets rejected.

The counterpart to the rejection tests is a positive control: the honest feature set must build
cleanly. A checker that rejected everything would pass every "does it catch cheating" test ever
written, and would also be useless.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

import numpy as np

from energy_platform.connectors.synthetic import BERLIN
from energy_platform.forecasting.vintage import observable_at

_HOUR_MS: Final = 3_600_000


class FeatureKind(StrEnum):
    """Where a feature's value came from, which determines when it became knowable."""

    CALENDAR = "calendar"
    SOLAR_GEOMETRY = "solar_geometry"
    VINTAGE = "vintage"
    LAGGED_ACTUAL = "lagged_actual"


class LookaheadError(RuntimeError):
    """Raised when a feature would be read before it existed.

    Carries the offending feature and both instants, because the useful question on seeing this is
    always "by how much, and which one" -- a feature late by an hour is a boundary bug, one late by
    five days is a lag that was not applied.
    """

    def __init__(self, feature: str, available_at_ms: int, issue_ms: int) -> None:
        self.feature = feature
        self.available_at_ms = available_at_ms
        self.issue_ms = issue_ms
        late_hours = (available_at_ms - issue_ms) / _HOUR_MS
        super().__init__(
            f"feature {feature!r} would be read {late_hours:.1f} h before it existed: available at "
            f"{_iso(available_at_ms)}, but the prediction is issued at {_iso(issue_ms)}"
        )


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Feature:
    """One feature value, with the instant it became knowable.

    ``available_at_ms=None`` means "knowable arbitrarily far ahead" -- reserved for the calendar and
    solar geometry. Anything derived from observed or forecast data must carry a real instant; the
    registry check in the test suite asserts that nothing quietly defaults to ``None``.
    """

    name: str
    kind: FeatureKind
    value: float | None
    available_at_ms: int | None = None

    def __post_init__(self) -> None:
        if self.kind in _MUST_DECLARE_AVAILABILITY and self.available_at_ms is None:
            raise ValueError(
                f"feature {self.name!r} is a {self.kind.value} feature and must declare "
                "available_at_ms; only calendar and solar-geometry features are knowable ahead"
            )


_MUST_DECLARE_AVAILABILITY: Final = frozenset({FeatureKind.VINTAGE, FeatureKind.LAGGED_ACTUAL})


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """The features behind one predicted hour."""

    ts_utc_ms: int
    issue_ms: int
    horizon_hour: int
    features: tuple[Feature, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(feature.name for feature in self.features)


def check_no_lookahead(row: FeatureRow) -> None:
    """Raise if any feature in ``row`` postdates the row's issue time."""
    for feature in row.features:
        if feature.available_at_ms is not None and feature.available_at_ms > row.issue_ms:
            raise LookaheadError(feature.name, feature.available_at_ms, row.issue_ms)


def build_matrix(rows: Sequence[FeatureRow]) -> tuple[tuple[str, ...], np.ndarray]:
    """Check every row, then stack them into a float matrix with ``NaN`` for missing values.

    ``NaN`` rather than an imputed value: ``HistGradientBoostingRegressor`` routes missing values
    down a learned branch, so a gap stays a gap all the way into the model. Imputing here would be
    the "NaN over fabrication" invariant quietly breaking at the last step before the answer.

    Feature order is taken from the first row and asserted identical for the rest -- a matrix whose
    columns mean different things in different rows is the kind of bug that produces plausible
    numbers forever.
    """
    if not rows:
        return (), np.empty((0, 0), dtype=float)

    names = rows[0].names
    for row in rows:
        check_no_lookahead(row)
        if row.names != names:
            raise ValueError(
                f"feature names differ between rows at {_iso(row.ts_utc_ms)}: "
                f"{names} vs {row.names}"
            )

    matrix = np.array(
        [[np.nan if f.value is None else f.value for f in row.features] for row in rows],
        dtype=float,
    )
    return names, matrix


# -- The honest feature set --------------------------------------------------------------


def calendar_features(ts_utc_ms: int) -> tuple[Feature, ...]:
    """Local-calendar position. Knowable arbitrarily far ahead.

    Local rather than UTC because the thing being predicted is local: the load profile is indexed
    by Europe/Berlin wall-clock hour and by weekday, which is also how M3's generator builds it.
    ``weekday`` and ``is_weekend`` are derived here rather than in dbt -- ``berlin_calendar`` emits
    only ts/date/hour, and widening it would touch every model that calls it for the benefit of one.
    """
    local = datetime.fromtimestamp(ts_utc_ms / 1000, UTC).astimezone(BERLIN)
    return (
        Feature("local_hour", FeatureKind.CALENDAR, float(local.hour)),
        Feature("weekday", FeatureKind.CALENDAR, float(local.weekday())),
        Feature("is_weekend", FeatureKind.CALENDAR, float(local.weekday() >= 5)),
        Feature("month", FeatureKind.CALENDAR, float(local.month)),
        Feature("day_of_year", FeatureKind.CALENDAR, float(local.timetuple().tm_yday)),
    )


def vintage_features(
    values: Mapping[str, float | None], vintage_issue_ms: int
) -> tuple[Feature, ...]:
    """Weather from the selected vintage, knowable at that vintage's issue instant."""
    return tuple(
        Feature(f"fc_{name}", FeatureKind.VINTAGE, value, available_at_ms=vintage_issue_ms)
        for name, value in sorted(values.items())
    )


def lagged_actual_feature(
    name: str, value: float | None, observed_ts_ms: int, lag_hours: int
) -> Feature:
    """An actual from the past, knowable only once its hour closed and the lag elapsed.

    The instant comes from :func:`~energy_platform.forecasting.vintage.observable_at` rather than
    from a local ``ts + lag``: the availability a feature *declares* and the availability the
    baselines *walk back to* have to be the same function, or the check passes on an arithmetic the
    data was never selected under.
    """
    return Feature(
        name,
        FeatureKind.LAGGED_ACTUAL,
        value,
        available_at_ms=observable_at(observed_ts_ms, lag_hours),
    )


def feature_names(rows: Iterable[FeatureRow]) -> tuple[str, ...]:
    """The column names of the first row, for stamping onto a run's provenance record."""
    for row in rows:
        return row.names
    return ()
