"""The only module in the dashboard that opens a connection or writes SQL.

**THE MART-ONLY RULE.** The dashboard is a presentation layer and nothing else: every number on
screen is a column of a warehouse mart, and the app never re-derives one. Reshaping is fine --
pivoting a long table wide, sorting, formatting a float as euros. Computing a cost, a rate, a
coverage figure or a metric is not. When a page needs a number the warehouse does not have, the
fix is a dbt model, not an expression here. M9's ``mart_coverage_monthly`` exists because of
exactly that rule: the monthly coverage grain did not exist, and adding a ``groupby`` in Python
would have put a number on screen that no test in the repo could check.

That is the last of four claims the repo makes layer by layer -- ingestion is bit-reproducible,
transformations live in tested SQL, optimisation is value-stable, presentation computes nothing --
and it is worth exactly as much as its enforcement, so it is enforced three ways:

* **The database refuses.** The app connects as ``dashboard_ro``, which holds SELECT on
  ``analytics_marts`` and nothing else. ``raw``, ``derived`` and the staging and intermediate
  schemas are not merely unread, they are unreadable, and the session is read-only besides. See
  ``dashboard/sql/grant_read_only.sql``.
* **A static guard.** ``tests/dashboard/test_mart_only.py`` walks the AST of every dashboard
  module and fails if any module other than this one imports psycopg or holds a SQL string.
* **A contract guard.** Every query below is declared as data -- a mart name and the columns it
  selects -- rather than as a SQL string, so the same test can check every one of those columns
  against ``dbt/target/manifest.json``. A column this app wants and the warehouse does not have
  fails the build, which is precisely the moment the rule says "change the mart".

The declarative form is what makes the third guard exact rather than a regex over SQL text. It is
also why ``where`` and ``order_by`` may only mention columns listed in ``columns``: an undeclared
column in a predicate would be a column the contract check never sees.

Connection handling follows the package's existing idiom -- ``PostgresConfig`` for env precedence
and ``psycopg.connect(dsn)`` per call, no pool and no ORM, exactly as ``scripts/report_regret.py``
and the CLI do it.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from typing import Any, Final

import psycopg
import streamlit as st
from psycopg import sql

from energy_platform.config import PostgresConfig

# How long a page may serve numbers it read earlier. The warehouse is rebuilt by a human running
# `just warehouse`, not by a stream, so a short TTL costs a query and buys a reload that actually
# reflects the rebuild. Explicit rather than defaulted because an unbounded cache would make a
# rebuilt warehouse look unchanged until the process restarted.
CACHE_TTL_SECONDS: Final = 60


@dataclass(frozen=True, slots=True)
class MartQuery:
    """A declared read of one mart: which relation, which columns, which rows, in what order.

    Deliberately not a SQL string. The columns are the dashboard's contract with the warehouse,
    and stating them as data is what lets ``tests/dashboard/test_mart_only.py`` check every one
    against the dbt manifest without parsing SQL.

    ``where`` and ``order_by`` may reference only columns named in ``columns`` -- the contract
    check enforces that, because a predicate on an undeclared column is a dependency the check
    would never see. ``where`` uses ``%s`` placeholders; values are bound by psycopg, never
    interpolated.
    """

    mart: str
    columns: tuple[str, ...]
    where: str | None = None
    order_by: tuple[str, ...] = ()

    def statement(self, schema: str) -> sql.Composed:
        """Compose the SELECT. Schema and relation go through Identifier, never an f-string."""
        parts: list[sql.Composable] = [
            sql.SQL("select {columns} from {relation}").format(
                columns=sql.SQL(", ").join(sql.Identifier(column) for column in self.columns),
                relation=sql.Identifier(schema, self.mart),
            )
        ]
        if self.where:
            parts.append(sql.SQL(" where ") + sql.SQL(self.where))
        if self.order_by:
            parts.append(
                sql.SQL(" order by {}").format(
                    sql.SQL(", ").join(sql.Identifier(column) for column in self.order_by)
                )
            )
        return sql.Composed(parts)


# --------------------------------------------------------------------------------------------
# The declared reads. One entry per question a page asks; the key is what the page calls it.
#
# Column lists are wider than any single chart needs on purpose: a page that wants a figure's
# completeness context should not have to add a query to get it, and every euro column here is
# paired with the coverage columns it must be read against.
# --------------------------------------------------------------------------------------------

# The coverage vocabulary is shared but not identical, and the difference is meaningful. The
# economics marts count PRICED hours -- hours where every cost term was present, because an hour
# missing a price cannot contribute to a bill. mart_coverage_monthly counts PRESENT hours, because
# it is about ingestion and knows nothing about pricing. Collapsing them to one name would make the
# denominators look interchangeable when they are not.
_PRICED_COVERAGE: Final = (
    "expected_hours",
    "covered_hours",
    "priced_hours",
    "gap_hours",
    "completeness_ratio",
    "is_partial_month",
)

_PRESENT_COVERAGE: Final = (
    "expected_hours",
    "covered_hours",
    "present_hours",
    "gap_hours",
    "completeness_ratio",
    "is_partial_month",
)

QUERIES: Final[dict[str, MartQuery]] = {
    # -- Overview -----------------------------------------------------------------------------
    "data_mode": MartQuery(
        mart="mart_data_quality",
        columns=("data_mode",),
        # Non-null exactly on telemetry rows, which is what "what is this warehouse made of"
        # means. The classification itself is the telemetry_data_mode macro's job, not ours.
        where="data_mode is not null",
        order_by=("data_mode",),
    ),
    "data_quality": MartQuery(
        mart="mart_data_quality",
        columns=(
            "source",
            "dataset",
            "region",
            "resolution",
            "data_mode",
            "expected_hours",
            "present_hours",
            "gap_hours",
            "null_count",
            "min_ts_utc",
            "max_ts_utc",
            "last_fetched_at",
        ),
        order_by=("source", "dataset"),
    ),
    "coverage_monthly": MartQuery(
        mart="mart_coverage_monthly",
        columns=("local_month", "source", "dataset", "region", "data_mode", *_PRESENT_COVERAGE),
        order_by=("local_month", "source", "dataset"),
    ),
    "hourly_energy": MartQuery(
        mart="mart_hourly_energy",
        columns=(
            "ts_utc",
            "local_ts",
            "local_date",
            "local_hour",
            "region",
            "pv_production_kwh",
            "household_load_kwh",
            "battery_charge_kwh",
            "battery_discharge_kwh",
            "soc_frac",
            "grid_import_kwh",
            "grid_export_kwh",
            "price_eur_mwh",
            "balance_residual_kwh",
        ),
        where="region = %s and local_date >= %s and local_date <= %s",
        order_by=("ts_utc",),
    ),
    "energy_days": MartQuery(
        mart="mart_hourly_energy",
        columns=("local_date", "region"),
        order_by=("local_date",),
    ),
    # -- Economics ----------------------------------------------------------------------------
    "tariff_counterfactuals": MartQuery(
        mart="mart_tariff_counterfactuals",
        columns=(
            "local_month",
            "region",
            "scenario",
            "tariff_id",
            "tariff_kind",
            "grid_import_kwh",
            "grid_export_kwh",
            "battery_unreturned_kwh",
            "energy_cost_eur",
            "base_fee_eur",
            "total_cost_eur",
            "feed_in_revenue_eur",
            "net_cost_eur",
            *_PRICED_COVERAGE,
        ),
        order_by=("local_month", "tariff_id", "scenario"),
    ),
    "solar_economics": MartQuery(
        mart="mart_solar_economics",
        columns=(
            "local_month",
            "region",
            "scenario",
            "tariff_id",
            "tariff_kind",
            "pv_production_kwh",
            "household_load_kwh",
            "self_consumed_kwh",
            "grid_import_kwh",
            "grid_export_kwh",
            "self_consumption_rate",
            "autarky_rate",
            "battery_unreturned_kwh",
            "avoided_grid_cost_eur",
            "feed_in_revenue_eur",
            "solar_value_eur",
            *_PRICED_COVERAGE,
        ),
        order_by=("local_month", "tariff_id", "scenario"),
    ),
    # -- Dispatch -----------------------------------------------------------------------------
    "dispatch_regret": MartQuery(
        mart="mart_dispatch_regret",
        columns=(
            "window_start",
            "window_end",
            "region",
            "tariff_id",
            "is_simulated",
            "not_simulated_reason",
            "sim_start",
            "sim_end",
            "simulated_days",
            "fallback_days",
            "clipped_hours",
            "naive_cost_eur",
            "forecast_driven_cost_eur",
            "perfect_foresight_cost_eur",
            "hindsight_cost_eur",
            "regret_eur",
            "forecast_error_cost_eur",
            "myopia_cost_eur",
            "available_savings_eur",
            "realised_savings_eur",
            "attainable_savings_eur",
            "captured_value_share",
            "attainable_value_share",
            "training_data_source",
            "expected_hours",
            "priced_hours",
            "gap_hours",
            "completeness_ratio",
            "is_partial_window",
        ),
        order_by=("window_start", "tariff_id"),
    ),
    "forward_dispatch_daily": MartQuery(
        mart="mart_forward_dispatch_daily",
        columns=(
            "window_start",
            "window_end",
            "region",
            "tariff_id",
            "scenario",
            "local_date",
            "sim_day",
            "plan_status",
            "soc_start_kwh",
            "soc_end_kwh",
            "clipped_hours",
            "pv_model_age_days",
            "net_cost_eur",
            "cumulative_net_cost_eur",
            "priced_hours",
            "expected_hours",
            "is_partial_day",
            "is_simulated",
        ),
        where="window_start = %s and tariff_id = %s and is_simulated",
        order_by=("scenario", "sim_day"),
    ),
    "dispatch_comparison": MartQuery(
        mart="mart_dispatch_comparison",
        columns=(
            "window_start",
            "window_end",
            "region",
            "tariff_id",
            "scenario",
            "energy_cost_eur",
            "feed_in_revenue_eur",
            "net_cost_eur",
            "terminal_value_eur",
            "adjusted_net_cost_eur",
            "battery_charge_kwh",
            "battery_discharge_kwh",
            "battery_unreturned_kwh",
            "savings_vs_naive_continuous_eur",
            "savings_vs_no_battery_eur",
            "solver",
            "solver_status",
            "expected_hours",
            "priced_hours",
            "gap_hours",
            "completeness_ratio",
            "is_partial_window",
        ),
        # adjusted_net_cost_eur is the ONLY column scenarios may be ranked by -- the mart says so,
        # and ordering by it here means no page has to remember.
        order_by=("window_start", "tariff_id", "adjusted_net_cost_eur"),
    ),
    # -- Forecasts ----------------------------------------------------------------------------
    "forecast_eval": MartQuery(
        mart="mart_forecast_eval",
        columns=(
            "site",
            "target",
            "model_key",
            "role",
            "window_start",
            "window_end",
            "horizon_hour",
            "training_data_source",
            "telemetry_lag_hours",
            "scored_hours",
            "mae_kwh",
            "bias_kwh",
            "rmse_kwh",
            "pinball_p10_kwh",
            "pinball_p50_kwh",
            "pinball_p90_kwh",
            "interval_coverage_p10_p90",
        ),
        # role first so a reader meets the label before the leaderboard, not after it.
        order_by=("target", "role", "model_key", "horizon_hour"),
    ),
}

#: Every mart the dashboard may touch. Derived from the declared queries rather than written out
#: again, so the two cannot disagree about what "the marts this app reads" means.
MARTS: Final[frozenset[str]] = frozenset(query.mart for query in QUERIES.values())


@dataclass(frozen=True, slots=True)
class WarehouseStatus:
    """Which of the marts this app reads currently exist.

    Reported by probing ``information_schema`` rather than by catching ``UndefinedTable`` from a
    failed query. The difference matters: swallowing that exception would render a mart whose name
    this app got *wrong* as a friendly "no data yet", hiding a bug behind the onboarding message.
    A probe distinguishes "the warehouse has not been built" from "this app asked for something
    that does not exist", and only the first is a state a stranger should be reassured about.
    """

    present: frozenset[str]
    missing: tuple[str, ...]
    reachable: bool = True
    error: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.reachable and not self.missing


def connection_config() -> PostgresConfig:
    """Postgres settings for the dashboard's own, read-only, credentials.

    Everything except the credentials comes from ``PostgresConfig.from_env()``, so the dashboard
    resolves host, port, database and the marts schema through the same precedence chain
    (``ENERGY_PG_*`` over ``DAGSTER_POSTGRES_*`` over defaults) as the rest of the platform, and a
    relocated warehouse moves for all of them at once.

    The user and password are then replaced, defaulting to ``dashboard_ro``. That role is the
    first of the three mart-only enforcement layers and the only one that holds at runtime: it can
    read ``analytics_marts`` and nothing else, and its sessions are read-only. Connecting as
    ``dagster`` would leave the rule to the two static guards, which cannot see a query that a
    future page composes at runtime.
    """
    base = PostgresConfig.from_env()
    return dataclasses.replace(
        base,
        user=os.environ.get("ENERGY_DASHBOARD_PG_USER") or "dashboard_ro",
        password=os.environ.get("ENERGY_DASHBOARD_PG_PASSWORD") or "dashboard_ro",
    )


def _connect() -> psycopg.Connection[Any]:
    config = connection_config()
    return psycopg.connect(config.dsn, connect_timeout=5)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def warehouse_status() -> WarehouseStatus:
    """Probe which declared marts exist, without assuming the database is up at all.

    An unreachable database is a distinct, reportable state rather than a traceback: the first
    thing a stranger does after `just demo` is open the dashboard, and Postgres may still be
    coming up behind the healthcheck.
    """
    schema = connection_config().marts_schema
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select table_name from information_schema.tables where table_schema = %s",
                (schema,),
            )
            found = {row[0] for row in cur.fetchall()}
    except psycopg.Error as exc:
        return WarehouseStatus(
            present=frozenset(),
            missing=tuple(sorted(MARTS)),
            reachable=False,
            error=str(exc).strip(),
        )
    present = MARTS & found
    return WarehouseStatus(present=present, missing=tuple(sorted(MARTS - found)))


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch(name: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Run a declared query and return its rows as dicts.

    ``name`` indexes ``QUERIES``; there is no path here that runs SQL the module did not declare.
    Rows come back as dicts in the shape ``scripts/report_regret.py`` already uses, so a caller
    can hand them straight to a DataFrame without a column-order convention to remember.
    """
    query = QUERIES[name]
    schema = connection_config().marts_schema
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(query.statement(schema), params)
        columns = [column.name for column in cur.description or ()]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def marts_for(*names: str) -> tuple[str, ...]:
    """The marts a set of declared queries reads -- what a page needs present to render."""
    return tuple(sorted({QUERIES[name].mart for name in names}))
