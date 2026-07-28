"""Shared page furniture: the data-mode banner, the empty-warehouse state, coverage callouts.

Every page starts with :func:`page_header`, which draws the banner and then reports whether the
marts that page needs are actually there. A page that gets ``False`` renders guidance and stops.

**The empty warehouse is the happy path, not an edge case.** A stranger clones the repo, runs
``just demo``, and opens the dashboard before anything has been seeded -- that is the first thing
that happens to this app, every time, and a traceback there is the difference between "polished"
and "works on the author's machine". So the no-data state is designed rather than defended
against, and ``tests/dashboard/test_empty_warehouse.py`` asserts every page reaches it cleanly.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from dashboard import format as fmt
from dashboard import warehouse

#: The commands that take an empty database to a full one, in the only order that works. `just
#: warehouse` alone is not enough on a fresh clone -- it builds marts from a raw zone that must
#: already hold data, which is what `just dbt-seed` puts there (offline, from recorded fixtures).
SEED_COMMANDS = (
    "just dbt-seed     # offline demo data into the raw zone\njust warehouse    # build every mart"
)

_MODE_TEXT: dict[str, str] = {
    "demo_synthetic": "Demo mode — synthetic data",
    "real": "Real mode — household telemetry",
}

_MODE_DETAIL: dict[str, str] = {
    "demo_synthetic": (
        "Every figure below comes from the M3 synthetic generator, not from a real house. "
        "The shapes are plausible and the pipeline is identical; the numbers are not a "
        "measurement of anything."
    ),
    "real": "Figures below come from real household telemetry.",
}


def active_data_mode() -> str | None:
    """The warehouse's own account of what it was built from, or None if it cannot say.

    Read from ``mart_data_quality.data_mode``, which the ``telemetry_data_mode`` dbt macro
    populates on telemetry rows. Deliberately not inferred here from a connector name or from a
    config flag: the banner is a number-on-screen like any other, and the mart-only rule applies
    to it. If the warehouse holds both synthetic and real telemetry, that is a genuine mixed state
    and is reported as such rather than resolved by picking one.
    """
    rows = warehouse.fetch("data_mode")
    modes: set[str] = {str(row["data_mode"]) for row in rows if row["data_mode"]}
    if not modes:
        return None
    if len(modes) > 1:
        return "mixed"
    return modes.pop()


def render_banner() -> None:
    """The persistent data-mode banner. Drawn by every page, so it cannot be navigated away from."""
    status = warehouse.warehouse_status()
    if not status.reachable:
        st.error("**No warehouse connection** — the database is not reachable yet.", icon="⚠️")
        return
    if "mart_data_quality" in status.missing:
        st.info("**No data yet** — the warehouse has not been built.", icon="📦")
        return

    mode = active_data_mode()
    if mode is None:
        st.info(
            "**Data mode unknown** — the warehouse holds no telemetry yet, so it cannot say "
            "whether it is running on synthetic or real data.",
            icon="📦",
        )
    elif mode == "mixed":
        st.warning(
            "**Mixed data mode** — this warehouse holds both synthetic and real telemetry. "
            "Set the `telemetry_source` dbt var to select one.",
            icon="⚠️",
        )
    else:
        st.caption(f"**{_MODE_TEXT.get(mode, mode)}** — {_MODE_DETAIL.get(mode, '')}")


def render_no_data(missing: tuple[str, ...]) -> None:
    """The onboarding state. Names the commands, because "no data" without them is a dead end."""
    st.subheader("No data yet")
    st.markdown(
        "This dashboard reads the warehouse and computes nothing itself, so until the warehouse "
        "is built there is nothing to show. Two commands from a clean clone:"
    )
    st.code(SEED_COMMANDS, language="bash")
    st.caption(
        "Missing marts: " + ", ".join(f"`{mart}`" for mart in missing) + ". "
        "Both commands are offline — they seed from recorded fixtures and need no API keys."
    )


def page_header(title: str, subtitle: str, needs: tuple[str, ...]) -> bool:
    """Draw the banner and title; report whether this page's marts are present.

    ``needs`` is a tuple of mart names, normally from ``warehouse.marts_for(...)`` so that the
    page's stated requirements and its actual queries come from one place.

    Returns False when the page cannot render, having already explained why. Callers stop there.
    """
    st.title(title)
    render_banner()
    st.caption(subtitle)

    status = warehouse.warehouse_status()
    if not status.reachable:
        st.subheader("No warehouse connection")
        st.markdown(
            "The dashboard could not reach Postgres. If the stack is still starting this "
            "resolves itself; otherwise check that the database is up and that the "
            "`dashboard_ro` role exists (`just dashboard-grants`)."
        )
        if status.error:
            st.code(status.error, language="text")
        return False

    absent = tuple(mart for mart in needs if mart in status.missing)
    if absent:
        render_no_data(absent)
        return False
    return True


def render_empty_selection(what: str = "this selection") -> None:
    """Marts exist but hold no rows for what was asked. A different state from "not built yet"."""
    st.info(
        f"The warehouse is built, but it has no rows for {what}. "
        "The demo covers three short windows rather than a continuous history.",
        icon="🗓️",
    )


def coverage_callout(row: dict[str, Any]) -> None:
    """Put a partial period's completeness beside the figures it qualifies, if it is partial."""
    note = fmt.completeness_note(row)
    if note:
        st.caption(f"⚠️ {note}")


def battery_reset_caveat() -> None:
    """The caveat that has to travel with every battery-vs-no-battery comparison.

    The synthetic generator resets state of charge at Berlin midnight, so the battery scenario
    discards stored energy that was bought and paid for. ``battery_unreturned_kwh`` is that
    quantity, carried by three marts precisely so the caveat is checkable rather than folklore.
    """
    st.caption(
        "⚠️ On synthetic data the generator resets state of charge at midnight, so the battery "
        "scenario discards energy it paid for. `battery_unreturned_kwh` measures it — treat "
        "battery-vs-no-battery differences as an upper bound on the battery's cost, not a "
        "measurement of its value."
    )
