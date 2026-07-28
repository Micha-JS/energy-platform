"""The dashboard's four pages, each a standalone Streamlit script.

Standalone rather than functions imported by the entrypoint, so ``AppTest.from_file`` can render
any one of them in isolation -- which is what lets the empty-warehouse test assert that every page
degrades gracefully, not just whichever one navigation happens to open first.
"""
