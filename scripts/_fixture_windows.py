"""The Europe/Berlin day windows the recorded test fixtures must cover -- one definition.

The SMARD and Open-Meteo recorders both need fixtures for the exact same days: a normal reference
day plus the two ~week-long windows straddling the 2024 DST-transition Sundays. These windows are a
fixture *data contract* -- a change here must move both fixture sets together -- so they live in one
place both recorders import, rather than in two copies that can silently drift.
"""

from __future__ import annotations

from datetime import date, timedelta

# A normal reference day plus the two windows straddling the 2024 DST-transition Sundays
# (2024-03-31 spring-forward, 2024-10-27 fall-back). Both DST Sundays fall inside their window.
NORMAL_DAY = date(2024, 6, 12)
MARCH_WINDOW = (date(2024, 3, 28), date(2024, 4, 3))
OCTOBER_WINDOW = (date(2024, 10, 24), date(2024, 10, 30))

# M7 needs a training set. Two disjoint DST weeks give a backtester almost nothing -- a
# weekday-seasonal baseline gets one prior equivalent day, and expanding-window CV gets a handful
# of folds -- so the fixtures extend to a contiguous spring quarter.
#
# It starts the day the March window ends rather than replacing it, and that is deliberate: both
# existing declared windows stay byte-identical, so every number M5 and M6 published about them
# stays true and only new rows appear beside them. It also cannot overlap, which matters because
# int_hourly_spine generates its grid per window with no UNION -- an overlap would silently double
# every hour in the intersection.
#
# NOTE this window swallows NORMAL_DAY, whose archive fixture is one of the two curated files
# carrying a hand-injected null (see fixtures/MANIFEST.md). The recorders skip those, so the gap
# survives and 2024-06 stays one hour short of complete -- on purpose, and stated in the README, so
# it reads as the coverage machinery working rather than as a bug.
SPRING_WINDOW = (date(2024, 4, 4), date(2024, 6, 30))


def expand_days(*spans: tuple[date, date]) -> list[date]:
    """Every calendar day in the given inclusive ``(start, end)`` spans, in order, deduplicated.

    Deduplicated because the spans are allowed to overlap -- ``NORMAL_DAY`` sits inside
    ``SPRING_WINDOW`` -- and a recorder asking for the same day twice would do the same work twice
    and log a misleading total.
    """
    seen: set[date] = set()
    out: list[date] = []
    for start, end in spans:
        day = start
        while day <= end:
            if day not in seen:
                seen.add(day)
                out.append(day)
            day += timedelta(days=1)
    return out
