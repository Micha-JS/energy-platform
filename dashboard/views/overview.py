"""Overview: the hourly energy balance over the covered windows, and how covered they are.

The two halves belong on one page on purpose. The balance chart is the platform's raw material,
and the coverage panel is the reason no figure anywhere in this app may be read without it: the
demo covers three short windows, so "June" here is not a June-shaped amount of anything.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from dashboard import charts, chrome, warehouse
from dashboard import format as fmt

NEEDS = warehouse.marts_for("energy_days", "coverage_monthly", "data_quality")

if chrome.page_header(
    "Overview",
    "Hourly energy balance over the covered windows, and the coverage behind every figure "
    "in this dashboard.",
    NEEDS,
):
    days = warehouse.fetch("energy_days")

    if not days:
        chrome.render_empty_selection("the hourly energy spine")
    else:
        regions = sorted({row["region"] for row in days})
        available = sorted({row["local_date"] for row in days})

        left, right = st.columns([1, 3])
        with left:
            region = st.selectbox("Site", regions)
        with right:
            # The demo's coverage is three DISJOINT spans, so a from/to range picker would happily
            # select a gap and then show an empty chart with no explanation. Picking a day and a
            # length keeps every selection inside data that exists.
            start = st.select_slider(
                "Start day",
                options=available,
                value=available[0],
                format_func=fmt.day_label,
            )
            span = st.slider("Days to show", min_value=1, max_value=14, value=7)

        end = min(available[-1], start + timedelta(days=span - 1))
        rows = warehouse.fetch("hourly_energy", (region, start, end))

        if not rows:
            chrome.render_empty_selection(f"{fmt.day_label(start)} - {fmt.day_label(end)}")
        else:
            st.altair_chart(charts.energy_balance(rows), width="stretch")
            if any(row["soc_frac"] is not None for row in rows):
                st.altair_chart(charts.state_of_charge(rows), width="stretch")

            hours_shown = len(rows)
            gaps = sum(1 for row in rows if row["balance_residual_kwh"] is None)
            columns = st.columns(3)
            columns[0].metric("Hours shown", f"{hours_shown:,}")
            columns[1].metric("Hours with an incomplete balance", f"{gaps:,}")
            columns[2].metric("Site", region)
            st.caption(
                "`balance_residual_kwh` is the AC-node identity "
                "`(pv + import + discharge) - (load + export + charge)`; it is NULL wherever a "
                "term is missing rather than zero-filled, which is why the middle number counts "
                "hours rather than fabricating a balance for them."
            )

    st.divider()
    st.subheader("Coverage by month")
    st.markdown(
        "Three numbers, three different questions. **Expected** is how long the calendar month "
        "is. **Covered** is how much of it the declared coverage windows ever claimed. "
        "**Present** is how much actually arrived. Covered = 0 means the pipeline never ran "
        "there; covered = 168 with present = 0 would mean it said it did and nothing landed."
    )

    coverage = warehouse.fetch("coverage_monthly")
    if not coverage:
        chrome.render_empty_selection("monthly coverage")
    else:
        st.altair_chart(charts.coverage_strip(coverage), width="stretch")

        partial_months = sorted({row["local_month"] for row in coverage if row["is_partial_month"]})
        if partial_months:
            st.caption(
                "⚠️ Partial months: "
                + ", ".join(fmt.month_label(month) for month in partial_months)
                + ". Any euro figure attributed to these is a total over the hours present, not a "
                "month's bill."
            )

        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Month": fmt.month_label(row["local_month"]),
                        "Source": row["source"],
                        "Dataset": row["dataset"],
                        "Expected": row["expected_hours"],
                        "Covered": row["covered_hours"],
                        "Present": row["present_hours"],
                        "Gap": row["gap_hours"],
                        "Complete": fmt.share_percent(row["completeness_ratio"]),
                        "Partial": "yes" if row["is_partial_month"] else "no",
                    }
                    for row in coverage
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    st.divider()
    st.subheader("Per-source freshness")
    quality = warehouse.fetch("data_quality")
    if not quality:
        chrome.render_empty_selection("ingestion sources")
    else:
        today = date.today()
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Source": row["source"],
                        "Dataset": row["dataset"],
                        "Mode": row["data_mode"] or fmt.MISSING,
                        "Expected": row["expected_hours"],
                        "Present": row["present_hours"],
                        "Gap": row["gap_hours"],
                        "Nulls": row["null_count"],
                        "Last fetched": (
                            fmt.day_label(row["last_fetched_at"])
                            if row["last_fetched_at"]
                            else fmt.MISSING
                        ),
                    }
                    for row in quality
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            f"`data_mode` is populated only on telemetry sources — prices and weather are neither "
            f"synthetic nor real household data. Today is {fmt.day_label(today)}; the demo's "
            "recorded fixtures are from 2024, so a large freshness lag is expected here."
        )
