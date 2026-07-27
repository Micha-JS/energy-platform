"""Battery dispatch optimizer (MILP, HiGHS via PuLP).

Answers M6's question -- *what would a perfectly-dispatched battery have cost?* -- over each
declared coverage window, under each consumption tariff, against three baselines.

The physics comes from the same :class:`~energy_platform.config.BatteryConfig` M3's synthetic
generator simulates under, and the money from the same :mod:`energy_platform.tariffs` engine M5's
marts price with, so the savings figure is a like-for-like comparison by construction rather than
by assertion. See :mod:`.optimizer` for why the formulation is a MILP rather than an LP, and
:mod:`.store` for why the raw zone's content-hash contract deliberately does not extend to
optimisation results.
"""

from energy_platform.dispatch.model import (
    DispatchHour,
    DispatchResult,
    HourFlows,
    HourInputs,
    Scenario,
    round_eur,
)
from energy_platform.dispatch.optimizer import (
    DispatchError,
    RawSolution,
    solve_raw,
    solve_window,
    solver_version,
)
from energy_platform.dispatch.pricing import (
    TERMINAL_VALUE_ENV,
    WindowPrices,
    settle_window,
    terminal_value_eur_kwh,
    window_prices,
)
from energy_platform.dispatch.runner import WindowSolution, solve
from energy_platform.dispatch.windows import (
    DBT_PROJECT_PATH_ENV,
    DEFAULT_DBT_PROJECT_PATH,
    CoverageWindow,
    dbt_project_path,
    load_coverage_windows,
)

__all__ = [
    "DBT_PROJECT_PATH_ENV",
    "DEFAULT_DBT_PROJECT_PATH",
    "TERMINAL_VALUE_ENV",
    "CoverageWindow",
    "DispatchError",
    "DispatchHour",
    "DispatchResult",
    "HourFlows",
    "HourInputs",
    "RawSolution",
    "Scenario",
    "WindowPrices",
    "WindowSolution",
    "dbt_project_path",
    "load_coverage_windows",
    "round_eur",
    "settle_window",
    "solve",
    "solve_raw",
    "solve_window",
    "solver_version",
    "terminal_value_eur_kwh",
    "window_prices",
]
