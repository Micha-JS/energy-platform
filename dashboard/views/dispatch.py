"""Dispatch: the three-way comparison, with captured-value share front and centre.

THE HEADLINE ON THIS PAGE IS NEGATIVE, and it is presented as the result it is. Of the savings a
perfectly-informed battery schedule would have produced, forecast-driven dispatch captured a large
negative share on the seeded demo data -- it lost money. That is not a bug and not a disappointment
to be buried under a friendlier metric: the prize over the covered windows is a fraction of a euro,
and the decomposition beside it shows that a planner given *perfect* forecasts captures almost none
of it either. The value is structurally out of reach of a one-day horizon, not lost to model error.

Which is why this page shows ``captured_value_share`` next to ``attainable_value_share`` and never
alone. A share on its own invites "the forecasts are bad"; the pair says where the value actually
went.

``captured_value_share`` is bounded above by 1 (hindsight <= forecast_driven is a theorem, so a
value above 1 is a bug) and deliberately unbounded below. Nothing here clamps it.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import charts, chrome, warehouse
from dashboard import format as fmt

NEEDS = warehouse.marts_for("dispatch_regret", "forward_dispatch_daily", "dispatch_comparison")

#: Which mart column supplies each bar of the savings chart. Selecting a column per scenario is a
#: reshape; computing a difference would not be. See charts.savings_vs_naive.
_SAVINGS_COLUMNS = {
    "forecast_driven": "realised_savings_eur",
    "perfect_foresight_plan": "attainable_savings_eur",
    "optimal": "available_savings_eur",
}

if chrome.page_header(
    "Dispatch",
    "Naive self-consumption, forecast-driven dispatch and the hindsight optimum — per window "
    "and per tariff.",
    NEEDS,
):
    regret = warehouse.fetch("dispatch_regret")
    simulated = [row for row in regret if row["is_simulated"]]
    unsimulated = [row for row in regret if not row["is_simulated"]]

    if not regret:
        chrome.render_empty_selection("the dispatch comparison")
    elif not simulated:
        chrome.render_empty_selection("any simulated window")
    else:
        labels = {
            f"{fmt.window_label(row['window_start'], row['window_end'])} · {row['tariff_id']}": row
            for row in simulated
        }
        choice = st.selectbox("Window and tariff", list(labels))
        row = labels[choice]

        st.subheader("Captured value")
        columns = st.columns(3)
        columns[0].metric(
            "Captured value share",
            fmt.share_percent(row["captured_value_share"]),
            help="realised_savings_eur / available_savings_eur. Bounded above by 1; "
            "deliberately not bounded below.",
        )
        columns[1].metric(
            "Attainable with perfect forecasts",
            fmt.share_percent(row["attainable_value_share"]),
            help="What the same day-ahead controller would have captured given the actuals. "
            "The ceiling no forecasting work could lift.",
        )
        columns[2].metric(
            "Size of the prize",
            fmt.euro(row["available_savings_eur"], places=4),
            help="naive_cost_eur - hindsight_cost_eur over the simulated span.",
        )

        captured = row["captured_value_share"]
        attainable = row["attainable_value_share"]
        if captured is not None and captured < 0:
            st.warning(
                f"**Forecast-driven dispatch lost money here.** It captured "
                f"{fmt.share_percent(captured)} of a prize worth "
                f"{fmt.euro(row['available_savings_eur'], places=4)} over "
                f"{row['simulated_days']} simulated days. Read it beside the attainable share: a "
                f"planner handed the actuals captures "
                f"{fmt.share_percent(attainable)}, so the value is mostly out of reach of a "
                f"one-day horizon rather than lost to forecast error.",
                icon="📉",
            )

        st.caption(
            "**Quote the share, not the euro figure.** A euro total over "
            f"{row['simulated_days']} days is not comparable to one over a year, or between a "
            "sunny window and a dark one; the share normalises by the size of the prize. "
            f"Simulated span {fmt.window_label(row['sim_start'], row['sim_end'])} — shorter than "
            "the window, because the forecast models need warm-up before anything can be planned."
        )
        chrome.coverage_callout(row)

        st.subheader("Savings against naive dispatch")
        st.altair_chart(
            charts.savings_vs_naive(
                [
                    {"scenario": scenario, "savings_eur": row[column]}
                    for scenario, column in _SAVINGS_COLUMNS.items()
                ]
            ),
            width="stretch",
        )

        st.subheader("Where the regret went")
        split = st.columns(3)
        split[0].metric("Total regret", fmt.euro(row["regret_eur"], places=4))
        split[1].metric("Forecast error", fmt.euro(row["forecast_error_cost_eur"], places=4))
        split[2].metric("Day-ahead myopia", fmt.euro(row["myopia_cost_eur"], places=4))
        st.caption(
            "`regret_eur = forecast_driven - hindsight`, split into the part better forecasts "
            "could remove (`forecast_error_cost_eur`) and the price of deciding one day at a "
            "time (`myopia_cost_eur`). The split is the difference between "
            "*our forecasts cost €X* and *deciding daily costs €Y and our forecasts cost €X on "
            "top* — very different claims about where the remaining value is."
        )

        detail = st.columns(3)
        detail[0].metric("Simulated days", f"{row['simulated_days']:,}")
        detail[1].metric(
            "Fallback days",
            f"{row['fallback_days']:,}",
            help="Days with no usable forecast, dispatched naively. A large value means this "
            "comparison is measuring the fallback, not the forecast.",
        )
        detail[2].metric("Clipped hours", f"{row['clipped_hours']:,}")

        st.subheader("Cumulative cost, day by day")
        daily = warehouse.fetch("forward_dispatch_daily", (row["window_start"], row["tariff_id"]))
        if not daily:
            chrome.render_empty_selection("this window's daily trajectory")
        else:
            st.altair_chart(charts.cumulative_cost(daily), width="stretch")
            st.caption(
                "`cumulative_net_cost_eur` is a running total computed in the mart, not "
                "accumulated here — this chart plots a column and adds nothing to it."
            )

    if unsimulated:
        st.divider()
        st.subheader("Windows that could not be simulated")
        st.caption(
            "Kept as rows rather than dropped: a window that simply vanished would let every "
            "coverage check downstream pass over something nobody could see was absent."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Window": fmt.window_label(item["window_start"], item["window_end"]),
                        "Region": item["region"],
                        "Reason": item["not_simulated_reason"],
                    }
                    for item in unsimulated
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    st.divider()
    st.subheader("Hindsight-optimal dispatch (M6)")
    comparison = warehouse.fetch("dispatch_comparison")
    if not comparison:
        chrome.render_empty_selection("the four-scenario comparison")
    else:
        st.caption(
            "Ranked by `adjusted_net_cost_eur` — the only column these scenarios may be ranked "
            "by, because it settles the energy each one left in the battery at the window's end. "
            "Comparing `net_cost_eur` would reward a scenario for finishing with a full battery "
            "it never paid to fill."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Window": fmt.window_label(item["window_start"], item["window_end"]),
                        "Tariff": item["tariff_id"],
                        "Scenario": fmt.scenario_label(item["scenario"]),
                        "Net cost": fmt.euro(item["net_cost_eur"]),
                        "Terminal value": fmt.euro(item["terminal_value_eur"], places=4),
                        "Adjusted net cost": fmt.euro(item["adjusted_net_cost_eur"]),
                        "vs naive": fmt.euro(item["savings_vs_naive_continuous_eur"], places=4),
                        "vs no battery": fmt.euro(item["savings_vs_no_battery_eur"]),
                        "Solver": item["solver"],
                        "Complete": fmt.share_percent(item["completeness_ratio"]),
                    }
                    for item in comparison
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        chrome.battery_reset_caveat()
