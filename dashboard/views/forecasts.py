"""Forecasts: model-versus-baseline evaluation, with the oracle visibly set apart.

READ ``role`` BEFORE RANKING ANYTHING. On the synthetic demo the flat-plate PV model
(``toy_physical``) is the process that *generated the labels* — M3's generator produced PV from
irradiance with that very model — so it carries ``role='oracle'`` and beating the field is a
tautology, not a result. A leaderboard sorted by MAE across all roles produces a winner that means
nothing, which is why this page splits the table by role rather than sorting one table by error.

Baselines are the credibility test, not filler: persistence and seasonal-naive are what a model
has to clear before "it works" means anything. They are drawn in muted grey as the reference line
they are.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import charts, chrome, warehouse
from dashboard import format as fmt

NEEDS = warehouse.marts_for("forecast_eval")

_ROLE_NOTE = {
    "oracle": (
        "**Oracle — not a result.** This is the process that generated the labels on synthetic "
        "data. Its error is a floor imposed by the generator, and it is excluded from any ranking."
    ),
    "baseline": (
        "**Baselines.** Persistence and seasonal-naive. A model that does not clear these has not "
        "demonstrated anything."
    ),
    "model": "**Models.** The fitted forecasters, the only rows a ranking is meaningful over.",
}

if chrome.page_header(
    "Forecasts",
    "Day-ahead forecast accuracy against the naive baselines, per horizon hour.",
    NEEDS,
):
    rows = warehouse.fetch("forecast_eval")

    if not rows:
        chrome.render_empty_selection("the forecast evaluation")
    else:
        targets = sorted({row["target"] for row in rows})
        target = st.selectbox("Target", targets)
        selected = [row for row in rows if row["target"] == target]

        source = {row["training_data_source"] for row in selected}
        if "synthetic" in source:
            st.info(
                "These models were trained and scored on **synthetic** data. Where a model is "
                "labelled `oracle` below, it is the generator's own model and its score is a "
                "tautology — the deliverable in this milestone is the backtesting harness, not "
                "an accuracy number.",
                icon="🔬",
            )

        st.altair_chart(charts.forecast_error_by_horizon(selected), width="stretch")
        st.caption(
            "One line per model, coloured by role. Role is a label, not a rank — the colours "
            "distinguish a model from a baseline from the oracle, and imply no ordering between "
            "them."
        )

        for role in ("model", "baseline", "oracle"):
            role_rows = [row for row in selected if row["role"] == role]
            if not role_rows:
                continue
            st.subheader(role.capitalize())
            st.markdown(_ROLE_NOTE[role])
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Model": row["model_key"],
                            "Horizon": row["horizon_hour"],
                            "Scored hours": row["scored_hours"],
                            "MAE": fmt.kwh(row["mae_kwh"], places=4),
                            "Bias": fmt.kwh(row["bias_kwh"], places=4),
                            "RMSE": fmt.kwh(row["rmse_kwh"], places=4),
                            "Pinball p10": fmt.kwh(row["pinball_p10_kwh"], places=4),
                            "Pinball p50": fmt.kwh(row["pinball_p50_kwh"], places=4),
                            "Pinball p90": fmt.kwh(row["pinball_p90_kwh"], places=4),
                            "p10-p90 coverage": fmt.share_percent(row["interval_coverage_p10_p90"]),
                        }
                        for row in role_rows
                    ]
                ),
                hide_index=True,
                width="stretch",
            )

        st.caption(
            "Pinball columns are empty for point-forecast models: a model that makes no interval "
            "claim does not get a perfect interval score. `p10-p90 coverage` targets 0.80 and is "
            "reported rather than claimed. `scored_hours` counts hours with both a prediction and "
            "an actual — a gap is never scored as a zero."
        )
