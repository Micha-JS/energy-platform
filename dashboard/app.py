"""Streamlit entrypoint. Page configuration and navigation; no data access of its own.

Run with ``streamlit run dashboard/app.py`` from the repository root, or via ``just dashboard``.

Navigation is explicit ``st.navigation`` over ``dashboard/views/`` rather than Streamlit's magic
``pages/`` directory. Two reasons, and the second is the one that matters: the order and titles are
stated here rather than inferred from filenames, and each view stays a plain script that
``AppTest.from_file`` can run on its own -- which is how ``tests/dashboard/`` renders all four
pages against both a seeded and an empty warehouse.

The data-mode banner is drawn by each view through ``chrome.page_header`` rather than once here,
so it is present whether a page is reached through this entrypoint or rendered standalone. It
cannot be navigated away from, and it cannot be tested away from either.
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Energy Data Platform",
    page_icon="⚡",
    layout="wide",
)

navigation = st.navigation(
    [
        # Paths are relative to this entrypoint, which is how st.Page resolves them.
        st.Page("views/overview.py", title="Overview", icon="📈", default=True),
        st.Page("views/economics.py", title="Economics", icon="💶"),
        st.Page("views/dispatch.py", title="Dispatch", icon="🔋"),
        st.Page("views/forecasts.py", title="Forecasts", icon="🔮"),
    ]
)

with st.sidebar:
    st.caption(
        "Every number on these pages is a column of a dbt mart. The app reshapes and formats; "
        "it computes nothing. If a figure is missing, the fix is a mart."
    )

navigation.run()
