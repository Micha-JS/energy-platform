"""Reading one day's forward plan out of the derived layer.

**Reads ``derived`` directly, and that is deliberate rather than lazy.** There is no mart over
``forward_dispatch_schedule`` -- M8's two marts summarise runs and days, not hours -- and the
dashboard's read-only role is explicitly denied ``derived`` because presentation must go through
marts. The publisher is not presentation: it emits a machine contract, and the hourly plan is the
contract. Adding a mart purely to launder the same rows through a second copy would give the plan
two representations free to drift, which is precisely what M10 spent the M8 change avoiding.

**Ambiguity is refused, never guessed.** A Berlin day exists under one run per consumption tariff,
so "the plan for the 12th" is under-specified until a tariff is named. The publisher takes the
configured one and fails loudly when the resolved (site, day, tariff) has no simulated run, naming
what to set -- the same posture ``assert_single_telemetry_source_per_site`` takes in the warehouse,
and for the same reason: a default here would publish a real, well-formed, wrong plan to a house.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

import psycopg
from psycopg import sql

from energy_platform.dispatch.forward import PLAN_STATUS_FALLBACK, PLAN_STATUS_PLANNED
from energy_platform.dispatch.windows import CoverageWindow
from energy_platform.publishing.contract import (
    PUBLISHED_SCENARIO,
    PlanCoverage,
    PlanHour,
    PlanProvenance,
    iso_date,
    iso_instant,
)
from energy_platform.rows import as_float

SCHEDULE_RELATION: Final = "forward_dispatch_schedule"
DAYS_RELATION: Final = "forward_dispatch_days"
RUNS_RELATION: Final = "forward_dispatch_runs"

# Plan statuses a day can carry and still be publishable. A `not_planned` day belongs to one of the
# reference scenarios and never reaches here; anything else is a value this module has not been
# taught about, and publishing it would be asserting something about a controller nobody wrote.
PUBLISHABLE_STATUSES: Final = frozenset({PLAN_STATUS_PLANNED, PLAN_STATUS_FALLBACK})


class PlanNotAvailableError(RuntimeError):
    """No publishable plan for the requested (site, day, tariff). Always says what to do next."""


@dataclass(frozen=True, slots=True)
class ForwardPlan:
    """One Berlin day's plan, as read. Everything the payload needs and nothing derived here."""

    site_id: str
    tariff_id: str
    local_date: date
    plan_status: str
    decision_time: datetime
    hours: tuple[PlanHour, ...]
    provenance: PlanProvenance

    @property
    def coverage(self) -> PlanCoverage:
        """Hours covered, with the expectation taken from the Berlin calendar.

        ``expected_hours`` comes from :class:`CoverageWindow`, so a DST day is 23 or 25 and a plan
        that resolved only nineteen hours is visibly short instead of quietly complete.
        """
        window = CoverageWindow(self.local_date, self.local_date)
        return PlanCoverage(
            local_date=iso_date(self.local_date),
            start_ts_utc=self.hours[0].ts_utc,
            end_ts_utc=self.hours[-1].ts_utc,
            expected_hours=window.expected_hours,
            planned_hours=sum(1 for hour in self.hours if hour.battery_charge_kwh is not None),
        )


class ForwardPlanReader:
    """Reads the published scenario's schedule for one Berlin day."""

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]], *, derived_schema: str):
        self._conn = conn
        self._derived = derived_schema

    def relations_exist(self) -> bool:
        """Whether M8 has ever run here. Distinguishes 'not built' from 'no plan for that day'."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = %s AND table_name IN (%s, %s, %s)
                """,
                (self._derived, SCHEDULE_RELATION, DAYS_RELATION, RUNS_RELATION),
            )
            row = cur.fetchone()
        return row is not None and int(str(row[0])) == 3

    def read_day(self, site_id: str, day: date, tariff_id: str) -> ForwardPlan:
        """The plan for one Berlin day, or a refusal that names the fix."""
        if not self.relations_exist():
            raise PlanNotAvailableError(
                f"{self._derived}.{SCHEDULE_RELATION} does not exist; run "
                "`just warehouse` (or `just forward-dispatch`) before publishing a plan"
            )

        day_row = self._read_day_row(site_id, day, tariff_id)
        if day_row is None:
            raise PlanNotAvailableError(
                f"no simulated {PUBLISHED_SCENARIO} plan for site '{site_id}' on {day} under "
                f"tariff '{tariff_id}'. The window may not be declared, the day may precede the "
                "forecast warm-up, or the tariff may not be the one that was simulated -- check "
                f"{self._derived}.{RUNS_RELATION}, and set ENERGY_TARIFF_ID or pass --tariff to "
                "select a different one."
            )
        plan_status, decision_time, provenance = day_row
        if plan_status not in PUBLISHABLE_STATUSES:
            raise PlanNotAvailableError(
                f"the plan for {day} has status '{plan_status}', which this publisher does not "
                f"know how to describe; expected one of {sorted(PUBLISHABLE_STATUSES)}"
            )

        hours = self._read_hours(site_id, day, tariff_id)
        if not hours:
            raise PlanNotAvailableError(
                f"the run for {day} exists but carries no hourly schedule; "
                f"rebuild with `just forward-dispatch`"
            )
        return ForwardPlan(
            site_id=site_id,
            tariff_id=tariff_id,
            local_date=day,
            plan_status=plan_status,
            decision_time=decision_time,
            hours=hours,
            provenance=provenance,
        )

    def _read_day_row(
        self, site_id: str, day: date, tariff_id: str
    ) -> tuple[str, datetime, PlanProvenance] | None:
        query = sql.SQL(
            """
            SELECT d.plan_status, d.decision_time,
                   r.input_digest, r.pv_model_key, r.load_model_key,
                   r.decision_rule_id, r.price_publication_rule_id, r.selection_rule_id,
                   r.training_data_source, r.solver, r.solver_version
            FROM {days} d
            JOIN {runs} r ON r.id = d.run_id
            WHERE d.scenario = %s AND d.region = %s AND d.tariff_id = %s AND d.local_date = %s
              AND r.is_simulated
            -- A day can sit inside more than one declared window only if the windows overlap,
            -- which dbt_project.yml forbids; ordering keeps the choice deterministic regardless.
            ORDER BY r.window_start DESC, r.id DESC
            LIMIT 1
            """
        ).format(
            days=sql.Identifier(self._derived, DAYS_RELATION),
            runs=sql.Identifier(self._derived, RUNS_RELATION),
        )
        with self._conn.cursor() as cur:
            cur.execute(query, (PUBLISHED_SCENARIO, site_id, tariff_id, day))
            row = cur.fetchone()
        if row is None:
            return None
        decision_time = row[1]
        assert isinstance(decision_time, datetime), f"decision_time was {decision_time!r}"
        provenance = PlanProvenance(
            input_digest=_text(row[2]),
            pv_model_key=_text(row[3]),
            load_model_key=_text(row[4]),
            decision_rule_id=_text(row[5]),
            price_publication_rule_id=_text(row[6]),
            selection_rule_id=_text(row[7]),
            training_data_source=_text(row[8]),
            solver=_text(row[9]),
            solver_version=_text(row[10]),
        )
        return str(row[0]), decision_time, provenance

    def _read_hours(self, site_id: str, day: date, tariff_id: str) -> tuple[PlanHour, ...]:
        # The Berlin day is derived from ts_utc rather than stored: forward_dispatch_schedule is
        # keyed in UTC, and deriving it here is what keeps a DST day 23 or 25 hours long instead of
        # whatever a naive date-range filter would have cut it to.
        query = sql.SQL(
            """
            SELECT s.ts_utc,
                   s.planned_battery_charge_kwh, s.planned_battery_discharge_kwh,
                   s.planned_grid_import_kwh, s.planned_grid_export_kwh, s.planned_soc_kwh,
                   s.planned_pv_production_kwh, s.planned_household_load_kwh,
                   s.import_price_ct_kwh
            FROM {schedule} s
            JOIN {runs} r ON r.id = s.run_id
            WHERE s.scenario = %s AND s.region = %s AND s.tariff_id = %s
              AND r.is_simulated
              AND (s.ts_utc AT TIME ZONE 'Europe/Berlin')::date = %s
            ORDER BY s.ts_utc
            """
        ).format(
            schedule=sql.Identifier(self._derived, SCHEDULE_RELATION),
            runs=sql.Identifier(self._derived, RUNS_RELATION),
        )
        with self._conn.cursor() as cur:
            cur.execute(query, (PUBLISHED_SCENARIO, site_id, tariff_id, day))
            rows = cur.fetchall()

        return tuple(_plan_hour(row) for row in rows)


def _plan_hour(row: tuple[object, ...]) -> PlanHour:
    """One published hour -- and the place the null-versus-zero rule is actually enforced.

    An hour the planner could not resolve still carries a stored ``planned_battery_charge_kwh`` of
    ``0.0``, because :func:`~energy_platform.dispatch.forward._naive_fallback` plans a hold for it
    and a hold is a real, feasible instruction the executor then carried out. In the warehouse that
    is correct. In a message to a house it is not: published as ``0.0`` it says "hold the battery
    this hour", which is a specific instruction about an hour nobody planned. The expectation
    columns are the honest signal -- they are null exactly when the solve did not resolve the hour
    -- so an hour without one carries no instruction either.

    Concretely, this was publishing a hold for the 23rd hour of a fallback day and counting it in
    ``planned_hours``, which is the conflation :mod:`energy_platform.publishing.contract` opens by
    forbidding.
    """
    resolved = row[5] is not None  # planned_soc_kwh: present iff the solve resolved this hour
    return PlanHour(
        ts_utc=iso_instant(_as_datetime(row[0])),
        battery_charge_kwh=as_float(row[1]) if resolved else None,
        battery_discharge_kwh=as_float(row[2]) if resolved else None,
        expected_grid_import_kwh=as_float(row[3]),
        expected_grid_export_kwh=as_float(row[4]),
        expected_soc_kwh=as_float(row[5]),
        expected_pv_production_kwh=as_float(row[6]),
        expected_household_load_kwh=as_float(row[7]),
        # Price is a property of the hour rather than of the plan, so it survives an unresolved
        # hour: the auction cleared whether or not the planner could use it.
        import_price_ct_kwh=as_float(row[8]),
    )


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _as_datetime(value: object) -> datetime:
    assert isinstance(value, datetime), f"expected a timestamptz, got {value!r}"
    return value
