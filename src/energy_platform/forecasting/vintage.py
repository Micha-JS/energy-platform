"""The vintage selection rule, and the observability arithmetic that mirrors it.

A backtest is only honest if every input to a prediction existed when the prediction was made.
Two things can violate that, and they are symmetric:

* **Reading a forecast that had not been issued yet.** Handled by :func:`select_vintage`.
* **Reading an actual that had not been observed yet.** Handled by :func:`observable_at` and
  :func:`latest_observable_equivalent`.

Both resolve against one instant per target day -- :func:`issue_time_ms` -- so there is a single
answer to "what was known" rather than one per feature.

**The rule, stated once.**

    For target Berlin day D, a prediction is issued at D 00:00 Europe/Berlin, and may use the last
    stored vintage whose ``issue_time`` is strictly before that instant.

Why this rule and not another: it is the information set M8 needs. A household planning day D's
battery schedule has, at the moment the day begins, exactly the forecasts issued before it. Pinning
the issue to the day boundary rather than to some hour inside D also avoids modelling intraday
re-planning, which M8 does not do -- a rule that promised more than the consumer uses would be
decoration.

``berlin_day_window`` supplies the boundary, so the same helper that generates the hourly spine and
sizes a :class:`~energy_platform.dispatch.windows.CoverageWindow` also decides what a forecast was
allowed to see. On a 23- or 25-hour day the boundary is still exactly one instant, and it is the
same instant everywhere.

:data:`SELECTION_RULE_ID` and :data:`PERSISTENCE_RULE_ID` are persisted on every prediction row, so
a stored forecast names the rule that produced it rather than relying on this docstring having been
true at the time.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Final

from energy_platform.connectors.synthetic import BERLIN
from energy_platform.orchestration.ingest import berlin_day_window

SELECTION_RULE_ID: Final = "last_vintage_before_target_day_start"
PERSISTENCE_RULE_ID: Final = "last_equivalent_hour_observable_at_issue"

_HOUR_MS: Final = 3_600_000

# Telemetry is hourly and an hour is stamped at the instant it *starts*, so the reading for
# ``ts`` summarises ``[ts, ts + 1h)`` and does not exist as a complete number until that interval
# closes. The observation lag is measured from there, not from the stamp -- see
# :func:`observable_at`, which is the only place this resolution is applied.
OBSERVATION_RESOLUTION_MS: Final = _HOUR_MS

# How far back any walk-until-observable may reach, in *calendar days*. Generous enough to cross
# the observation lag plus a run of gaps, bounded so a sparsely-covered site cannot silently reach
# back a season for a value.
#
# Days, not steps, and the difference is not cosmetic: bounding the step count makes the real reach
# depend on the stride, so the same "60" meant two months at a one-day stride and fourteen months at
# a seven-day one -- with both reported under the same rule id.
MAX_LOOKBACK_DAYS: Final = 60


def max_steps(stride_days: int) -> int:
    """Steps of ``stride_days`` inside :data:`MAX_LOOKBACK_DAYS`, and never fewer than one."""
    return max(1, MAX_LOOKBACK_DAYS // stride_days)


def issue_time_ms(target_day: date) -> int:
    """The instant a prediction for ``target_day`` is issued: Berlin midnight starting that day."""
    return berlin_day_window(target_day).start_ms


def select_vintage(issue_times_ms: Iterable[int], target_day: date) -> int | None:
    """The vintage a prediction for ``target_day`` may use, or ``None`` if none qualifies.

    Strictly before the day boundary, so a vintage issued *at* 00:00 on the target day itself is
    excluded. That is not a rounding decision: the recorded offline Open-Meteo fixture is stamped at
    exactly Berlin midnight of its issue day (``cli._berlin_midnight``), so an inclusive bound would
    quietly admit a forecast issued at the very instant the day it describes began.
    """
    cutoff = issue_time_ms(target_day)
    eligible = [issued for issued in issue_times_ms if issued < cutoff]
    return max(eligible) if eligible else None


def observable_at(ts_utc_ms: int, lag_hours: int) -> int:
    """When an actual for the hour *starting* at ``ts_utc_ms`` becomes knowable.

    Two terms, and dropping either one is a leak:

    * **The interval itself.** An hourly reading stamped at ``ts`` covers ``[ts, ts + 1h)``, so it
      is not a finished number until ``ts + 1h``. This term is what keeps a zero-lag platform
      honest: without it the hour beginning at the issue instant counts as already observed, and a
      fold would train on the first hour of the very day it is predicting.
    * **The reporting lag.** Not zero on this platform. Synthetic telemetry is generated from
      Open-Meteo archive weather, which is ERA5-backed and settles about five days late, so a
      synthetic hour cannot be observed until the weather behind it has been. See
      ``ForecastConfig.telemetry_lag_hours``; a real house reporting over Home Assistant has a lag
      of 0, which leaves the interval term alone rather than collapsing to the identity.

    Every caller resolves observability through here -- the training filter, the naive baselines and
    the lagged-actual feature's declared availability alike. The bug this signature prevents is the
    one that shipped: three call sites, each re-deriving ``ts + lag`` by hand, one of them wrong.
    """
    return ts_utc_ms + OBSERVATION_RESOLUTION_MS + lag_hours * _HOUR_MS


def is_observable(ts_utc_ms: int, issue_ms: int, lag_hours: int) -> bool:
    """Whether an actual for ``ts_utc_ms`` had been observed by ``issue_ms``."""
    return observable_at(ts_utc_ms, lag_hours) <= issue_ms


def equivalent_hour_ms(target_ts_utc_ms: int, days_back: int) -> int:
    """The same *local wall-clock hour*, ``days_back`` Berlin calendar days earlier.

    Stepping back in local days rather than in 24-hour blocks is what makes "the same hour last
    week" mean the same thing in the load profile, which is indexed by local hour -- across a DST
    switch the two differ by an hour, and it is the local reading that drives consumption.

    Two local times need a decision and get an explicit one. A wall clock that does not exist
    (spring-forward 02:00) is resolved forward by ``zoneinfo``'s default. A wall clock that occurs
    twice (fall-back 02:00) resolves to the first occurrence (``fold=0``). Both are arbitrary but
    neither is silent, and the alternative -- refusing to produce a baseline on two days a year --
    would put holes in the eval mart for a reason no reader could guess.
    """
    local = datetime.fromtimestamp(target_ts_utc_ms / 1000, UTC).astimezone(BERLIN)
    shifted = (local - timedelta(days=days_back)).replace(fold=0)
    return int(shifted.astimezone(UTC).timestamp() * 1000)


def latest_observable_equivalent(
    target_ts_utc_ms: int,
    *,
    issue_ms: int,
    lag_hours: int,
    stride_days: int,
    steps: int | None = None,
) -> int | None:
    """Walk back in ``stride_days`` steps to the most recent equivalent hour already observed.

    This is what makes the naive baselines honest on this platform. "Persistence" normally means
    *yesterday's* value, but yesterday has not been observed yet when the observation lag is five
    days -- so a literal D-1 baseline would be reading the future and would beat the models for the
    worst possible reason. Stepping back until the value is genuinely observable makes the baseline
    weaker and correct, which is the trade the whole milestone is about.

    Returns ``None`` if no equivalent hour within :data:`MAX_LOOKBACK_DAYS` (or an explicit
    ``steps``) is observable, rather than reaching arbitrarily far into the past for something to
    say.
    """
    for step in range(1, (max_steps(stride_days) if steps is None else steps) + 1):
        candidate = equivalent_hour_ms(target_ts_utc_ms, step * stride_days)
        if is_observable(candidate, issue_ms, lag_hours):
            return candidate
    return None
