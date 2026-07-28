"""The project's validated colour palette, shared by every rendering surface.

Two things draw the same scenarios: ``scripts/report_regret.py``, which produces the README's
three-way figure, and the M9 Streamlit dashboard's Dispatch page. Before this module they each
held their own hex strings, which is the arrangement where the figure and the page drift apart
and a reader who has learned that blue means "forecast-driven" learns it wrong somewhere else.
The colours live here so that cannot happen -- the same reason ``declared_coverage_hours`` is a
dbt macro rather than a query repeated in two marts.

**Colour is bound to the entity, never to its rank.** ``SCENARIO_COLOURS`` maps a scenario key to
a colour, so adding a fourth scenario or reordering a chart cannot repaint the existing three.
A palette indexed by position would silently recolour the deliverable the moment it stopped
coming first.

**Aqua sits below 3:1 against the light surface**, which is why the figure directly labels every
bar and the dashboard is required to do the same. That is the relief rule doing real work, not
decoration: the colour distinguishes, the label carries the value.

This module is deliberately stdlib-only -- string constants and a mapping, nothing more. It is
imported by ``scripts/report_regret.py`` (matplotlib) and by ``dashboard/charts.py`` (Altair),
neither of which may leak into the package: ``tests/test_import_containment.py`` fails if
anything under ``src/energy_platform`` imports a plotting or scientific library. Naming colours is
not plotting, so the constants can live here while both renderers stay outside.
"""

from __future__ import annotations

from typing import Final

# Validated categorical slots 1-3, in fixed order: the deliverable first, then the two references.
COLOUR_FORECAST_DRIVEN: Final = "#2a78d6"  # blue
COLOUR_PERFECT_PLAN: Final = "#eb6834"  # orange
COLOUR_HINDSIGHT: Final = "#1baf7a"  # aqua

# The fourth categorical slot, for the naive baseline where it is drawn as a series rather than as
# the zero line. The figure plots differences against naive and so never needs it; the dashboard's
# cumulative-cost chart plots all four scenarios and does.
COLOUR_NAIVE: Final = "#8a6fd4"  # violet

SURFACE: Final = "#fcfcfb"
INK_PRIMARY: Final = "#0b0b0b"
INK_SECONDARY: Final = "#52514e"
INK_MUTED: Final = "#898781"
GRIDLINE: Final = "#e1e0d9"
BASELINE: Final = "#c3c2b7"

# Sequential accents for coverage and completeness, kept apart from the categorical slots above so
# a coverage strip can never be mistaken for a scenario.
COLOUR_COMPLETE: Final = "#1baf7a"
COLOUR_PARTIAL: Final = "#d9a441"

#: Scenario key -> colour, keyed by the identifiers the marts use in their ``scenario`` columns
#: (``mart_forward_dispatch_daily``, ``mart_dispatch_regret``). Keying on the warehouse's own
#: vocabulary means a chart cannot invent a scenario name the data does not have.
SCENARIO_COLOURS: Final[dict[str, str]] = {
    "naive_continuous": COLOUR_NAIVE,
    "forecast_driven": COLOUR_FORECAST_DRIVEN,
    "perfect_foresight_plan": COLOUR_PERFECT_PLAN,
    "optimal": COLOUR_HINDSIGHT,
}

#: Scenario key -> the label a human should read. Same keys, same reason: the figure's legend and
#: the dashboard's legend say the same words about the same series.
SCENARIO_LABELS: Final[dict[str, str]] = {
    "naive_continuous": "Naive self-consumption",
    "forecast_driven": "Forecast-driven (executed)",
    "perfect_foresight_plan": "Same planner, perfect forecast",
    "optimal": "Hindsight-optimal",
}

#: Forecast-eval ``role`` -> colour. Roles are not ranks and must not be read as a scale:
#: ``oracle`` is the process that generated the labels on synthetic data, so it is set apart
#: rather than placed at the good end of a gradient. See ``mart_forecast_eval``'s description.
ROLE_COLOURS: Final[dict[str, str]] = {
    "model": COLOUR_FORECAST_DRIVEN,
    "baseline": INK_MUTED,
    "oracle": COLOUR_PERFECT_PLAN,
}
