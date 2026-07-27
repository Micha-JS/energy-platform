"""Reading the warehouse and writing the derived zone, for the backtester.

Same shape as M6's ``dispatch.store``: this module owns every piece of SQL, and hands frozen
dataclasses to a pure core that never sees a connection. That split is what lets the whole of
``tests/forecasting`` run with no Postgres.

**Same contract as the dispatch tables, for the same reason.** A fit is not bit-reproducible across
platforms, so append-with-content-hash would be claiming something false. These tables are
replace-on-rerun, and every row carries the provenance needed to attribute a divergence:
``config_hash`` over the fit's *inputs*, the library versions, the vintage-selection rule, and the
observation lag.

**Why it reads a staging view.** Forecast vintages come from ``stg_weather_forecast`` rather than
from ``raw.forecast_observations``, because ``tests/dbt/test_no_lookahead.py`` permits
exactly one dbt model to read that source and going around it in Python would honour the letter of
the guard while defeating its point. The staging view already carries ``source`` and both vintage
columns, so nothing is lost.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from energy_platform.config import ForecastConfig
from energy_platform.dispatch.windows import CoverageWindow
from energy_platform.rows import as_float, as_required_float

if TYPE_CHECKING:
    from energy_platform.forecasting.runner import ModelRun

OBSERVATIONS_RELATION: Final = "mart_hourly_energy"
VINTAGES_RELATION: Final = "stg_weather_forecast"

# Weather columns the feature builder consumes, in a stable order.
VINTAGE_COLUMNS: Final[tuple[str, ...]] = (
    "ghi_w_m2",
    "direct_radiation_w_m2",
    "diffuse_radiation_w_m2",
    "temperature_c",
    "cloud_cover_pct",
    "wind_speed_m_s",
)


class ForecastInputError(RuntimeError):
    """Raised when the warehouse cannot supply what a backtest needs, naming the fix."""


@dataclass(frozen=True, slots=True)
class Observation:
    """One hour of actuals -- the targets, and the history the baselines look back into."""

    ts_utc_ms: int
    local_date: date
    pv_production_kwh: float | None
    household_load_kwh: float | None


@dataclass(frozen=True, slots=True)
class VintageHour:
    """One (vintage, target hour) pair: what the forecast issued at ``issue_ms`` said about it."""

    issue_ms: int
    target_ts_utc_ms: int
    values: dict[str, float | None]


def _ddl(schema: str) -> list[sql.Composed]:
    identifier = sql.Identifier(schema)
    return [
        sql.SQL("create schema if not exists {schema}").format(schema=identifier),
        sql.SQL("""
            create table if not exists {schema}.forecast_runs (
                id bigint generated always as identity primary key,
                site text not null,
                target text not null,
                model_key text not null,
                role text not null,
                window_start date not null,
                window_end date not null,
                train_start date,
                train_end date,
                config_hash text not null,
                selection_rule_id text not null,
                persistence_rule_id text not null,
                telemetry_lag_hours integer not null,
                training_data_source text not null,
                forecast_source text not null,
                seed integer not null,
                n_train_rows integer not null,
                feature_names jsonb not null,
                hyperparameters jsonb,
                library_versions jsonb not null,
                artifact_key text,
                trained_at timestamptz not null default now()
            )
        """).format(schema=identifier),
        # One row per (site, target, model, window): a duplicate would double every metric the eval
        # mart aggregates, exactly as a duplicate dispatch run would double its totals.
        sql.SQL("""
            create unique index if not exists forecast_runs_key
            on {schema}.forecast_runs (site, target, model_key, window_start, window_end)
        """).format(schema=identifier),
        sql.SQL("""
            create table if not exists {schema}.forecast_predictions (
                run_id bigint not null references {schema}.forecast_runs (id) on delete cascade,
                ts_utc timestamptz not null,
                local_date date not null,
                horizon_hour integer not null,
                issue_time timestamptz not null,
                vintage_issue_time timestamptz,
                fold integer not null,
                p10_kwh double precision,
                p50_kwh double precision,
                p90_kwh double precision,
                actual_kwh double precision
            )
        """).format(schema=identifier),
        sql.SQL("""
            create index if not exists forecast_predictions_run_idx
            on {schema}.forecast_predictions (run_id, ts_utc)
        """).format(schema=identifier),
    ]


@dataclass(frozen=True, slots=True)
class WriteCounts:
    runs: int
    predictions: int
    replaced: int


class ForecastRepository:
    """Reads two dbt relations, writes two derived tables. Connection injected, as M6 does."""

    def __init__(
        self,
        conn: psycopg.Connection[tuple[object, ...]],
        *,
        derived_schema: str,
        marts_schema: str,
        staging_schema: str,
    ) -> None:
        self._conn = conn
        self._derived = derived_schema
        self._marts = marts_schema
        self._staging = staging_schema

    def ensure_schema(self) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            for statement in _ddl(self._derived):
                cur.execute(statement)

    def input_relations_exist(self) -> tuple[bool, bool]:
        """``(observations, vintages)``, separately, so an error can name the right fix."""
        with self._conn.cursor() as cur:
            cur.execute(
                "select to_regclass(%s), to_regclass(%s)",
                (f"{self._marts}.{OBSERVATIONS_RELATION}", f"{self._staging}.{VINTAGES_RELATION}"),
            )
            row = cur.fetchone()
        return (row is not None and row[0] is not None, row is not None and row[1] is not None)

    def regions(self, window: CoverageWindow) -> tuple[str, ...]:
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    select distinct region from {schema}.{relation}
                    where local_date between %s and %s order by region
                """).format(
                    schema=sql.Identifier(self._marts),
                    relation=sql.Identifier(OBSERVATIONS_RELATION),
                ),
                (window.start, window.end),
            )
            return tuple(str(row[0]) for row in cur.fetchall())

    def read_observations(self, start: date, end: date, region: str) -> tuple[Observation, ...]:
        """Actuals over an inclusive Berlin-date span.

        Filtered on ``local_date`` rather than ``ts_utc``, exactly as the optimiser's
        ``read_window`` does, so the span's edges land on Berlin midnights and a DST day contributes
        23 or 25 rows rather than a truncated 24.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    select (extract(epoch from ts_utc) * 1000)::bigint,
                           local_date, pv_production_kwh, household_load_kwh
                    from {schema}.{relation}
                    where region = %s and local_date between %s and %s
                    order by ts_utc
                """).format(
                    schema=sql.Identifier(self._marts),
                    relation=sql.Identifier(OBSERVATIONS_RELATION),
                ),
                (region, start, end),
            )
            return tuple(
                Observation(
                    ts_utc_ms=int(as_required_float(row[0])),
                    local_date=row[1],  # type: ignore[arg-type]
                    pv_production_kwh=as_float(row[2]),
                    household_load_kwh=as_float(row[3]),
                )
                for row in cur.fetchall()
            )

    def read_vintages(
        self, region: str, source: str, start: date, end: date
    ) -> tuple[VintageHour, ...]:
        """Every (vintage, target hour) pair whose *target* falls in the span, for one producer.

        Deliberately returns all vintages rather than pre-selecting: the selection rule lives in
        exactly one place (:func:`~energy_platform.forecasting.vintage.select_vintage`), and it is
        not this one. A repository that quietly filtered would be a second implementation of it.
        """
        columns = sql.SQL(", ").join(sql.Identifier(name) for name in VINTAGE_COLUMNS)
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    select (extract(epoch from issue_time) * 1000)::bigint,
                           (extract(epoch from target_ts_utc) * 1000)::bigint,
                           {columns}
                    from {schema}.{relation}
                    where region = %s and source = %s
                      and target_local_date between %s and %s
                    order by issue_time, target_ts_utc
                """).format(
                    columns=columns,
                    schema=sql.Identifier(self._staging),
                    relation=sql.Identifier(VINTAGES_RELATION),
                ),
                (region, source, start, end),
            )
            return tuple(
                VintageHour(
                    issue_ms=int(as_required_float(row[0])),
                    target_ts_utc_ms=int(as_required_float(row[1])),
                    values={
                        name: as_float(row[2 + offset])
                        for offset, name in enumerate(VINTAGE_COLUMNS)
                    },
                )
                for row in cur.fetchall()
            )

    def replace_window(
        self,
        scope: Sequence[tuple[str, str, date, date]],
        runs: Sequence[tuple[dict[str, object], list[dict[str, object]]]],
    ) -> WriteCounts:
        """Delete every run in ``scope`` and write ``runs`` in its place, in one transaction.

        Replace rather than upsert, and clear the whole ``(site, target, window)`` group before
        writing any of it: a partial rewrite would leave a mart aggregating predictions from two
        different fits of the same model.

        The delete is keyed on the *window*, not on ``(window, model_key)``. Keyed on the model, a
        model the harness stops emitting -- because a window turned out to be too short to judge it
        on, or because it was removed outright -- would keep its last rows in ``derived`` forever
        and keep appearing in ``mart_forecast_eval`` beside models that were actually re-run. A
        stale row that looks current is worse than a missing one.

        ``scope`` is what the caller *evaluated*, passed separately from what it *produced*, and
        that separation is the whole point. Deriving the delete keys from ``runs`` -- as this did --
        defeats the reasoning above at its limit: a re-run that emits nothing for a window (no
        eligible vintage left, no observations at all) deletes nothing, and the previous fit's rows
        go on being served as current. An empty ``runs`` against a non-empty ``scope`` is therefore
        a legitimate and load-bearing call, not a no-op to skip.
        """
        keys = list(dict.fromkeys(scope))
        outside = {
            (run["site"], run["target"], run["window_start"], run["window_end"]) for run, _ in runs
        } - set(keys)
        if outside:
            raise ForecastInputError(
                f"refusing to write runs outside the declared scope: {sorted(map(str, outside))}. "
                "A run written under a key that was never cleared would sit beside the rows it was "
                "meant to replace."
            )

        written_runs = written_predictions = replaced = 0
        with self._conn.transaction(), self._conn.cursor() as cur:
            for site, target, window_start, window_end in keys:
                cur.execute(
                    sql.SQL("""
                        delete from {schema}.forecast_runs
                        where site = %s and target = %s
                          and window_start = %s and window_end = %s
                    """).format(schema=sql.Identifier(self._derived)),
                    (site, target, window_start, window_end),
                )
                replaced += cur.rowcount if cur.rowcount > 0 else 0

            for run, predictions in runs:
                cur.execute(
                    sql.SQL("""
                        insert into {schema}.forecast_runs (
                            site, target, model_key, role, window_start, window_end,
                            train_start, train_end, config_hash, selection_rule_id,
                            persistence_rule_id, telemetry_lag_hours, training_data_source,
                            forecast_source, seed, n_train_rows, feature_names, hyperparameters,
                            library_versions, artifact_key
                        ) values (
                            %(site)s, %(target)s, %(model_key)s, %(role)s, %(window_start)s,
                            %(window_end)s, %(train_start)s, %(train_end)s, %(config_hash)s,
                            %(selection_rule_id)s, %(persistence_rule_id)s, %(telemetry_lag_hours)s,
                            %(training_data_source)s, %(forecast_source)s, %(seed)s,
                            %(n_train_rows)s, %(feature_names)s, %(hyperparameters)s,
                            %(library_versions)s, %(artifact_key)s
                        ) returning id
                    """).format(schema=sql.Identifier(self._derived)),
                    {
                        **run,
                        "feature_names": Jsonb(run["feature_names"]),
                        "hyperparameters": Jsonb(run["hyperparameters"]),
                        "library_versions": Jsonb(run["library_versions"]),
                    },
                )
                returned = cur.fetchone()
                if returned is None:  # pragma: no cover -- RETURNING on INSERT always yields a row
                    raise ForecastInputError("insert into forecast_runs returned no id")
                run_id = int(as_required_float(returned[0]))
                written_runs += 1

                if predictions:
                    cur.executemany(
                        sql.SQL("""
                            insert into {schema}.forecast_predictions (
                                run_id, ts_utc, local_date, horizon_hour, issue_time,
                                vintage_issue_time, fold, p10_kwh, p50_kwh, p90_kwh, actual_kwh
                            ) values (
                                %(run_id)s, to_timestamp(%(ts_utc_ms)s / 1000.0),
                                %(local_date)s, %(horizon_hour)s,
                                to_timestamp(%(issue_ms)s / 1000.0),
                                case when %(vintage_issue_ms)s is null then null
                                     else to_timestamp(%(vintage_issue_ms)s / 1000.0) end,
                                %(fold)s, %(p10_kwh)s, %(p50_kwh)s, %(p90_kwh)s, %(actual_kwh)s
                            )
                        """).format(schema=sql.Identifier(self._derived)),
                        [{**row, "run_id": run_id} for row in predictions],
                    )
                    written_predictions += len(predictions)

        return WriteCounts(runs=written_runs, predictions=written_predictions, replaced=replaced)


def run_payload(
    run: ModelRun,
    forecast: ForecastConfig,
    selection_rule_id: str,
    persistence_rule_id: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Flatten a :class:`~energy_platform.forecasting.runner.ModelRun` into the rows written here.

    Lives beside the SQL rather than in either caller, so the CLI and the Dagster asset write
    identical rows by construction -- the shared-core pattern applied to the write side. A second
    copy in one of the two entry points is exactly how the columns drift apart.
    """
    from energy_platform.forecasting.models import HYPERPARAMETERS, library_versions

    header: dict[str, object] = {
        "site": run.site,
        "target": run.target,
        "model_key": run.model_key,
        "role": run.role,
        "window_start": run.window_start,
        "window_end": run.window_end,
        "train_start": run.train_start,
        "train_end": run.train_end,
        "config_hash": run.config_hash,
        "selection_rule_id": selection_rule_id,
        "persistence_rule_id": persistence_rule_id,
        "telemetry_lag_hours": forecast.telemetry_lag_hours,
        "training_data_source": forecast.training_data_source,
        "forecast_source": forecast.forecast_source,
        "seed": forecast.seed,
        "n_train_rows": run.n_train_rows,
        "feature_names": list(run.feature_names),
        "hyperparameters": dict(HYPERPARAMETERS),
        "library_versions": library_versions(),
        # Null, and not as a placeholder: the backtest refits per fold and persists nothing, so
        # there is no artifact for a run to point at. The column exists for the serving path that
        # loads through `models.load_artifact`; writing a key here for a file that was never saved
        # would make the provenance record cite an artifact nobody can produce.
        "artifact_key": None,
    }
    predictions: list[dict[str, object]] = [
        {
            "ts_utc_ms": prediction.ts_utc_ms,
            "local_date": prediction.local_date,
            "horizon_hour": prediction.horizon_hour,
            "issue_ms": prediction.issue_ms,
            "vintage_issue_ms": prediction.vintage_issue_ms,
            "fold": prediction.fold,
            "p10_kwh": prediction.p10_kwh,
            "p50_kwh": prediction.p50_kwh,
            "p90_kwh": prediction.p90_kwh,
            "actual_kwh": prediction.actual_kwh,
        }
        for prediction in run.predictions
    ]
    return header, predictions
