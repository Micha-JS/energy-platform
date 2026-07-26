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


def expand_days(*spans: tuple[date, date]) -> list[date]:
    """Every calendar day in the given inclusive ``(start, end)`` spans, in order."""
    out: list[date] = []
    for start, end in spans:
        day = start
        while day <= end:
            out.append(day)
            day += timedelta(days=1)
    return out
