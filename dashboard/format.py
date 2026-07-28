"""Display formatting. Pure functions over values the warehouse already computed.

Nothing here derives a quantity -- these turn a number the marts produced into the string a human
reads. The distinction is the mart-only rule at its narrowest: ``share_percent`` multiplies by 100
because "0.067" and "6.7%" are the same number written two ways, whereas dividing one mart column
by another to *get* a share would be computing, and belongs in SQL.

Two conventions are enforced here rather than left to each page:

* **Shares are never clamped.** ``captured_value_share`` is bounded above by 1 (a theorem) and
  deliberately unbounded below, because forecast-driven dispatch can legitimately underperform the
  naive baseline -- on the seeded demo data it does, by a lot. A UI that clipped the axis or the
  value to [0, 1] would hide the single most interesting result in the project.
* **Missing stays missing.** NULL renders as an em dash, never as zero. A model with no interval
  claim has no pinball score, an unsimulated window has no cost, and a gap is not a zero; the
  warehouse is careful about that and the display has no licence to be less so.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

#: What a NULL looks like. An em dash reads as "there is no value here", where "0.00" reads as a
#: measurement -- see the module docstring.
MISSING = "—"

Number = float | int | Decimal | None


def _as_float(value: Number) -> float | None:
    if value is None:
        return None
    return float(value)


def euro(value: Number, places: int = 2) -> str:
    """Format euros. Negative values keep their sign: a negative net cost is a month that earned."""
    number = _as_float(value)
    if number is None:
        return MISSING
    return f"€{number:,.{places}f}"


def kwh(value: Number, places: int = 1) -> str:
    number = _as_float(value)
    if number is None:
        return MISSING
    return f"{number:,.{places}f} kWh"


def share_percent(value: Number, places: int = 1) -> str:
    """A [0, 1]-style fraction as a percentage, unclamped in both directions."""
    number = _as_float(value)
    if number is None:
        return MISSING
    return f"{number * 100:,.{places}f}%"


def hours(value: Number) -> str:
    number = _as_float(value)
    if number is None:
        return MISSING
    return f"{number:,.0f} h"


def month_label(value: date | datetime | None) -> str:
    if value is None:
        return MISSING
    return value.strftime("%b %Y")


def day_label(value: date | datetime | None) -> str:
    if value is None:
        return MISSING
    return value.strftime("%Y-%m-%d")


def window_label(start: date | None, end: date | None) -> str:
    if start is None or end is None:
        return MISSING
    return f"{day_label(start)} → {day_label(end)}"


def freshness(lag: timedelta | None) -> str:
    if lag is None:
        return MISSING
    total_hours = lag.total_seconds() / 3600
    if total_hours < 48:
        return f"{total_hours:,.0f} h ago"
    return f"{total_hours / 24:,.0f} d ago"


def completeness_note(row: dict[str, Any]) -> str | None:
    """The sentence a euro figure from this row must be read with, or None if it is complete.

    Every monetary total in this warehouse is a total over the hours that were actually present,
    and the seeded demo covers weeks rather than years -- so a monthly figure read without its
    completeness is a week's electricity mistaken for a month's bill. The marts carry the flag and
    the ratio precisely so no consumer has to derive them; this turns them into words.

    Accepts either coverage vocabulary: the monthly marts flag ``is_partial_month``, the
    window-grain ones ``is_partial_window``. Both mean the same thing about the same denominator.
    """
    partial = row.get("is_partial_month")
    if partial is None:
        partial = row.get("is_partial_window")
    if not partial:
        return None

    ratio = _as_float(row.get("completeness_ratio"))
    present = row.get("priced_hours")
    if present is None:
        present = row.get("present_hours")
    expected = row.get("expected_hours")

    covered = f"{share_percent(ratio, places=0)} covered" if ratio is not None else "partly covered"
    if present is not None and expected is not None:
        return f"Partial period — {covered} ({present:,.0f} of {expected:,.0f} hours)."
    return f"Partial period — {covered}."


def scenario_label(scenario: str) -> str:
    """Human wording for a mart ``scenario`` value, including the M5/M6 counterfactual pair."""
    from energy_platform.palette import SCENARIO_LABELS

    return SCENARIO_LABELS.get(scenario, scenario.replace("_", " ").capitalize())
