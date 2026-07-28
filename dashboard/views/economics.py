"""Economics: what the household would have paid, and what the sun earned.

Two marts, one question each. ``mart_tariff_counterfactuals`` answers "what would this house have
paid under tariff X, with and without the battery" across {static, dynamic} x {battery,
no_battery}. ``mart_solar_economics`` answers "what was the PV worth" -- self-consumption, autarky,
and the comparison that actually decides a German PV investment: a self-consumed kWh avoids the
import price, an exported one earns the statutory feed-in rate, and those are not close.

Every euro figure on this page is a total over the hours that were present, and every seeded
month is short. The completeness context travels with the figures rather than sitting in a
footnote, because a monthly total read as a monthly bill is wrong by a factor of four here.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import charts, chrome, warehouse
from dashboard import format as fmt

NEEDS = warehouse.marts_for("tariff_counterfactuals", "solar_economics")

if chrome.page_header(
    "Economics",
    "Tariff counterfactuals and solar economics, per month, with the coverage each figure "
    "must be read against.",
    NEEDS,
):
    counterfactuals = warehouse.fetch("tariff_counterfactuals")

    if not counterfactuals:
        chrome.render_empty_selection("tariff counterfactuals")
    else:
        months = sorted({row["local_month"] for row in counterfactuals})
        month = st.selectbox("Month", months, index=len(months) - 1, format_func=fmt.month_label)
        selected = [row for row in counterfactuals if row["local_month"] == month]

        st.subheader("What the month would have cost")
        st.caption(
            "`net_cost_eur` is energy plus the pro-rated standing charge, minus feed-in revenue. "
            "Negative means the month earned money. The standing charge is pro-rated by "
            "completeness so a whole month's fee is not charged against a week of energy — "
            "`base_fee_eur_month` keeps the un-pro-rated figure so that can be undone."
        )

        grid = st.columns(len(selected) or 1)
        for column, row in zip(grid, selected, strict=False):
            column.metric(
                f"{row['tariff_id']} · {row['scenario']}",
                fmt.euro(row["net_cost_eur"]),
                help=f"energy {fmt.euro(row['energy_cost_eur'])}, "
                f"base fee {fmt.euro(row['base_fee_eur'])}, "
                f"feed-in {fmt.euro(row['feed_in_revenue_eur'])}",
            )

        if selected:
            chrome.coverage_callout(selected[0])
        chrome.battery_reset_caveat()

        st.altair_chart(
            charts.monthly_cost(counterfactuals, "net_cost_eur", "net cost (€)"),
            width="stretch",
        )

        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Month": fmt.month_label(row["local_month"]),
                        "Tariff": row["tariff_id"],
                        "Scenario": row["scenario"],
                        "Import": fmt.kwh(row["grid_import_kwh"]),
                        "Export": fmt.kwh(row["grid_export_kwh"]),
                        "Energy": fmt.euro(row["energy_cost_eur"]),
                        "Base fee": fmt.euro(row["base_fee_eur"]),
                        "Feed-in": fmt.euro(row["feed_in_revenue_eur"]),
                        "Net cost": fmt.euro(row["net_cost_eur"]),
                        "Unreturned": fmt.kwh(row["battery_unreturned_kwh"]),
                        "Complete": fmt.share_percent(row["completeness_ratio"]),
                        "Partial": "yes" if row["is_partial_month"] else "no",
                    }
                    for row in counterfactuals
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    st.divider()
    st.subheader("What the sun earned")

    solar = warehouse.fetch("solar_economics")
    if not solar:
        chrome.render_empty_selection("solar economics")
    else:
        battery_rows = [row for row in solar if row["scenario"] == "battery"]
        latest = battery_rows[-1] if battery_rows else solar[-1]

        columns = st.columns(4)
        columns[0].metric("Self-consumption", fmt.share_percent(latest["self_consumption_rate"]))
        columns[1].metric("Autarky", fmt.share_percent(latest["autarky_rate"]))
        columns[2].metric("Avoided grid cost", fmt.euro(latest["avoided_grid_cost_eur"]))
        columns[3].metric("Feed-in revenue", fmt.euro(latest["feed_in_revenue_eur"]))
        st.caption(
            f"{fmt.month_label(latest['local_month'])}, {latest['scenario']} scenario. "
            "Self-consumption is `self_consumed / pv_production`; autarky is "
            "`self_consumed / household_load`. Both share a numerator defined as "
            "`load - import`, which is the meter's answer rather than `pv - export`."
        )
        chrome.coverage_callout(latest)

        st.markdown(
            "**Avoided cost against feed-in revenue** is the comparison that decides a German PV "
            "investment. A self-consumed kWh avoids the import price of the hour it happened in; "
            "an exported one earns the flat statutory rate. The gap between the two columns "
            "below is why self-consumption is worth engineering for."
        )
        st.altair_chart(charts.rate_over_months(solar), width="stretch")

        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Month": fmt.month_label(row["local_month"]),
                        "Tariff": row["tariff_id"],
                        "Scenario": row["scenario"],
                        "PV": fmt.kwh(row["pv_production_kwh"]),
                        "Load": fmt.kwh(row["household_load_kwh"]),
                        "Self-consumed": fmt.kwh(row["self_consumed_kwh"]),
                        "Self-consumption": fmt.share_percent(row["self_consumption_rate"]),
                        "Autarky": fmt.share_percent(row["autarky_rate"]),
                        "Avoided cost": fmt.euro(row["avoided_grid_cost_eur"]),
                        "Feed-in": fmt.euro(row["feed_in_revenue_eur"]),
                        "Solar value": fmt.euro(row["solar_value_eur"]),
                        "Complete": fmt.share_percent(row["completeness_ratio"]),
                        "Partial": "yes" if row["is_partial_month"] else "no",
                    }
                    for row in solar
                ]
            ),
            hide_index=True,
            width="stretch",
        )
