"""Altair specs. Every chart reads columns the marts already computed.

Altair rather than a new plotting stack: it ships inside Streamlit, so this adds no wheel the
container was not already carrying, and unlike ``st.line_chart`` it gives the encoding control
three of these charts genuinely need -- binding colour to a scenario key, setting a role apart
without implying a rank, and drawing a diverging axis around zero. Streamlit's native charts are
used directly where they suffice; these are the cases where they do not.

Colour comes from ``energy_platform.palette``, the same module ``scripts/report_regret.py`` draws
the README figure from, so blue means "forecast-driven" in both places by construction. Colour is
bound to the entity and never to its rank, and because aqua sits below 3:1 on this surface every
chart that can carry a direct label does.
"""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd

from energy_platform.palette import (
    COLOUR_COMPLETE,
    COLOUR_PARTIAL,
    GRIDLINE,
    INK_SECONDARY,
    ROLE_COLOURS,
    SCENARIO_COLOURS,
    SCENARIO_LABELS,
)

#: Height that keeps a chart legible in a Streamlit column without dominating the page.
_HEIGHT = 280


def _scenario_scale(keys: list[str]) -> alt.Scale:
    """Colour scale over scenario keys, mapping each key to its palette colour explicitly.

    An explicit domain/range pair rather than a categorical default: it is what makes the colour a
    property of the scenario rather than of its position, so filtering a scenario out of a chart
    cannot recolour the ones that remain.
    """
    known = [key for key in keys if key in SCENARIO_COLOURS]
    return alt.Scale(domain=known, range=[SCENARIO_COLOURS[key] for key in known])


def energy_balance(rows: list[dict[str, Any]]) -> alt.Chart:
    """Hourly PV, load, grid import and grid export over a covered span.

    Long-form reshape only -- the four series are four mart columns, unmodified.
    """
    frame = pd.DataFrame(rows)
    series = {
        "pv_production_kwh": "PV production",
        "household_load_kwh": "Household load",
        "grid_import_kwh": "Grid import",
        "grid_export_kwh": "Grid export",
    }
    melted = frame.melt(
        id_vars=["local_ts"],
        value_vars=list(series),
        var_name="series",
        value_name="kwh",
    )
    melted["series"] = melted["series"].map(series)
    return (
        alt.Chart(melted)
        .mark_line(strokeWidth=1.4)
        .encode(
            x=alt.X("local_ts:T", title="Europe/Berlin"),
            y=alt.Y("kwh:Q", title="kWh per hour"),
            color=alt.Color("series:N", title=None),
            tooltip=["local_ts:T", "series:N", alt.Tooltip("kwh:Q", format=".3f")],
        )
        .properties(height=_HEIGHT)
    )


def state_of_charge(rows: list[dict[str, Any]]) -> alt.Chart:
    """End-of-hour state of charge as a fraction. Its own chart because its unit is not kWh."""
    frame = pd.DataFrame(rows)
    return (
        alt.Chart(frame)
        .mark_area(opacity=0.5, color=SCENARIO_COLOURS["forecast_driven"])
        .encode(
            x=alt.X("local_ts:T", title="Europe/Berlin"),
            y=alt.Y(
                "soc_frac:Q",
                title="state of charge",
                axis=alt.Axis(format="%"),
                scale=alt.Scale(domain=[0, 1]),
            ),
            tooltip=["local_ts:T", alt.Tooltip("soc_frac:Q", format=".1%")],
        )
        .properties(height=180)
    )


def coverage_strip(rows: list[dict[str, Any]]) -> alt.Chart:
    """Coverage per month per series, coloured by whether the month is complete.

    Two colours, not a gradient: "complete" and "partial" is the distinction that changes how a
    figure must be read, and a continuous ramp would invite reading 0.92 and 0.94 as meaningfully
    different when the only thing that matters is that neither is 1.
    """
    frame = pd.DataFrame(rows)
    frame["month"] = pd.to_datetime(frame["local_month"])
    frame["completeness"] = frame["completeness_ratio"].astype(float)
    frame["state"] = frame["is_partial_month"].map({True: "Partial", False: "Complete"})
    return (
        alt.Chart(frame)
        .mark_rect(stroke=GRIDLINE, strokeWidth=0.5)
        .encode(
            x=alt.X("yearmonth(month):O", title=None),
            y=alt.Y("dataset:N", title=None),
            color=alt.Color(
                "state:N",
                title=None,
                scale=alt.Scale(
                    domain=["Complete", "Partial"], range=[COLOUR_COMPLETE, COLOUR_PARTIAL]
                ),
            ),
            tooltip=[
                alt.Tooltip("yearmonth(month):O", title="month"),
                "source:N",
                "dataset:N",
                alt.Tooltip("completeness:Q", format=".1%", title="covered"),
                alt.Tooltip("present_hours:Q", title="present hours"),
                alt.Tooltip("expected_hours:Q", title="expected hours"),
            ],
        )
        .properties(height=max(_HEIGHT, 22 * frame["dataset"].nunique()))
    )


def monthly_cost(rows: list[dict[str, Any]], value_column: str, title: str) -> alt.Chart:
    """Monthly euro figures grouped by tariff and battery scenario.

    Partial months are hatched by opacity rather than hidden: every seeded month is short, and a
    chart that dropped them would be empty, while one that drew them as if complete would lie.
    """
    frame = pd.DataFrame(rows)
    frame["month"] = pd.to_datetime(frame["local_month"])
    frame["series"] = frame["tariff_id"] + " · " + frame["scenario"]
    return (
        alt.Chart(frame)
        .mark_bar()
        .encode(
            x=alt.X("series:N", title=None, axis=alt.Axis(labelAngle=-40)),
            y=alt.Y(f"{value_column}:Q", title=title),
            color=alt.Color("scenario:N", title=None),
            opacity=alt.Opacity(
                "is_partial_month:N",
                title="partial month",
                scale=alt.Scale(domain=[False, True], range=[1.0, 0.55]),
            ),
            column=alt.Column("yearmonth(month):O", title=None),
            tooltip=[
                alt.Tooltip("yearmonth(month):O", title="month"),
                "tariff_id:N",
                "scenario:N",
                alt.Tooltip(f"{value_column}:Q", format=".2f", title=title),
                alt.Tooltip("completeness_ratio:Q", format=".1%", title="covered"),
            ],
        )
        .properties(height=_HEIGHT)
    )


def rate_over_months(rows: list[dict[str, Any]]) -> alt.Chart:
    """Self-consumption and autarky per month, as percentages on an axis anchored at zero."""
    frame = pd.DataFrame(rows)
    frame["month"] = pd.to_datetime(frame["local_month"])
    melted = frame.melt(
        id_vars=["month", "scenario", "tariff_id"],
        value_vars=["self_consumption_rate", "autarky_rate"],
        var_name="rate",
        value_name="value",
    )
    melted["rate"] = melted["rate"].map(
        {"self_consumption_rate": "Self-consumption", "autarky_rate": "Autarky"}
    )
    return (
        alt.Chart(melted)
        .mark_line(point=True, strokeWidth=1.8)
        .encode(
            x=alt.X("yearmonth(month):O", title=None),
            y=alt.Y("value:Q", title="rate", axis=alt.Axis(format="%")),
            color=alt.Color("rate:N", title=None),
            strokeDash=alt.StrokeDash("scenario:N", title=None),
            tooltip=[
                alt.Tooltip("yearmonth(month):O", title="month"),
                "scenario:N",
                "rate:N",
                alt.Tooltip("value:Q", format=".1%"),
            ],
        )
        .properties(height=_HEIGHT)
    )


def savings_vs_naive(rows: list[dict[str, Any]]) -> alt.Chart:
    """Savings against naive dispatch, drawn on the axis the README figure uses.

    Naive is the zero line rather than a fourth bar, for the reason
    ``scripts/report_regret.py`` states: over a covered span all four scenarios cost about the
    same -- they are the same house under the same tariff -- so four absolute bars are four
    identical bars. The question is not "what did it cost" but "what did dispatching differently
    change", and this is the axis that asks it.

    ``savings_eur`` is a mart column selected per scenario by the caller --
    ``realised_savings_eur``, ``attainable_savings_eur``, ``available_savings_eur`` -- never a
    difference computed here.
    Positive is better than naive; the seeded demo's forecast-driven bar is negative, and the axis
    is deliberately free to show that rather than clamped to the good half.
    """
    frame = pd.DataFrame(rows)
    frame["label"] = frame["scenario"].map(SCENARIO_LABELS).fillna(frame["scenario"])
    scale = _scenario_scale(frame["scenario"].tolist())

    bars = (
        alt.Chart(frame)
        .mark_bar()
        .encode(
            x=alt.X(
                "savings_eur:Q",
                title="€ saved vs naive dispatch (negative = worse than naive)",
            ),
            y=alt.Y("label:N", title=None, sort=list(frame["label"])),
            color=alt.Color("scenario:N", scale=scale, legend=None),
            tooltip=[
                "label:N",
                alt.Tooltip("savings_eur:Q", format=".4f", title="€ vs naive"),
            ],
        )
    )
    # Direct labels, because aqua does not clear 3:1 on this surface -- the relief rule.
    text = bars.mark_text(align="left", dx=4, fontSize=11).encode(
        text=alt.Text("savings_eur:Q", format="+.4f"), color=alt.value(INK_SECONDARY)
    )
    zero = (
        alt.Chart(pd.DataFrame({"zero": [0.0]}))
        .mark_rule(color=GRIDLINE, strokeWidth=1.4)
        .encode(x="zero:Q")
    )
    return (zero + bars + text).properties(height=160)


def cumulative_cost(rows: list[dict[str, Any]]) -> alt.Chart:
    """Cumulative settled cost per scenario over the simulated days.

    ``cumulative_net_cost_eur`` is precomputed by ``mart_forward_dispatch_daily`` -- this plots it
    and does not accumulate anything, which is the mart-only rule showing up as a running total
    that lives in SQL.
    """
    frame = pd.DataFrame(rows)
    frame["label"] = frame["scenario"].map(SCENARIO_LABELS).fillna(frame["scenario"])
    return (
        alt.Chart(frame)
        .mark_line(strokeWidth=1.6)
        .encode(
            x=alt.X("sim_day:Q", title="day of the simulation"),
            y=alt.Y("cumulative_net_cost_eur:Q", title="cumulative net cost (€)"),
            color=alt.Color(
                "scenario:N",
                title=None,
                scale=_scenario_scale(frame["scenario"].tolist()),
                legend=alt.Legend(labelExpr="datum.label"),
            ),
            tooltip=[
                "label:N",
                alt.Tooltip("sim_day:Q", title="day"),
                alt.Tooltip("cumulative_net_cost_eur:Q", format=".3f", title="cumulative €"),
            ],
        )
        .properties(height=_HEIGHT)
    )


def forecast_error_by_horizon(rows: list[dict[str, Any]]) -> alt.Chart:
    """MAE against horizon hour, with ``role`` carried in the encoding.

    Role is not a rank and is not drawn as one. On synthetic data the flat-plate PV model is the
    process that generated the labels, so it carries ``role='oracle'`` and beating the field is a
    tautology; it is dashed and set apart rather than placed at the good end of a scale. Baselines
    are muted grey because they are the reference the models must clear, not competitors.
    """
    frame = pd.DataFrame(rows)
    roles = [role for role in ("model", "baseline", "oracle") if role in set(frame["role"])]
    return (
        alt.Chart(frame)
        .mark_line(point=False, strokeWidth=1.6)
        .encode(
            x=alt.X("horizon_hour:Q", title="horizon (hours ahead)"),
            y=alt.Y("mae_kwh:Q", title="MAE (kWh)"),
            color=alt.Color(
                "role:N",
                title="role",
                scale=alt.Scale(domain=roles, range=[ROLE_COLOURS[role] for role in roles]),
            ),
            strokeDash=alt.StrokeDash("model_key:N", title="model"),
            tooltip=[
                "model_key:N",
                "role:N",
                alt.Tooltip("horizon_hour:Q", title="horizon"),
                alt.Tooltip("mae_kwh:Q", format=".4f", title="MAE"),
                alt.Tooltip("scored_hours:Q", title="scored hours"),
            ],
        )
        .properties(height=_HEIGHT)
    )
