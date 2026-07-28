"""Streamlit presentation layer for the energy platform.

Lives outside ``src/energy_platform`` on purpose. ``tests/test_import_containment.py`` fences the
scientific stack out of every first-party module beyond ``forecasting/``, and that fence is what
M3's cross-platform content-hash guarantee rests on -- so a package that imports pandas, altair and
streamlit sits here, exactly as ``scripts/report_regret.py`` sits outside for matplotlib.

The rule this package is built around is stated in ``dashboard/warehouse.py``: every number on
screen is a mart column, and the app computes nothing.
"""
