"""The published payload: what a plan says, and what it explicitly does not say.

**The payload is a recommendation, not a command.** That is not a disclaimer bolted onto a
docstring -- it is a field, ``kind``, carried in every message, because the difference matters to
whoever writes the automation on the other end and prose in a repository will not reach them. This
platform reads meters and publishes advice; it has no write path to an inverter, no command topic,
and no notion of acknowledgement. A consumer that chooses to actuate on this is making that
decision itself, and the payload is shaped so it cannot pretend otherwise.

**Versioned from the first message.** ``schema_version`` is in the body *and* in the topic. A
consumer pins a topic; a breaking change takes a new one, and the retained message on the old topic
keeps describing the contract it was written for rather than mutating under a running Home
Assistant instance.

**Nulls travel.** An hour the planner could not resolve publishes ``null``, never ``0.0``. Zero is
a legitimate instruction -- hold the battery -- so conflating it with "no plan" would tell a house
to do something specific on an hour nobody planned. Same rule as the warehouse's.

**Provenance is reused, not minted.** ``input_digest``, the model keys and the rule ids are the
tokens M7 and M8 already persist on the rows this payload is built from. A plan on a broker and the
run in ``derived.forward_dispatch_runs`` can therefore be tied together after the fact, which is
the only way to answer "why did it tell the house that?" a week later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

# Bumped only for a BREAKING change to the payload. Additive fields do not bump it: a consumer
# reading `hours[].battery_charge_kwh` is unaffected by a new sibling key, and forcing every
# subscriber onto a new topic for an addition would train them to ignore the version.
SCHEMA_VERSION: Final = 1

# The value of `kind`. A single word, in every message, in the position a consumer's parser will
# actually look -- see the module docstring for why this is a field and not a comment.
KIND_RECOMMENDATION: Final = "recommendation"

# The scenario a published plan comes from. `forecast_driven` is the only one that *is* a plan:
# the other three are reference trajectories computed for the regret decomposition, and two of them
# are hindsight constructions that could not have been known on the day.
PUBLISHED_SCENARIO: Final = "forecast_driven"


@dataclass(frozen=True, slots=True)
class PlanHour:
    """One hour of the plan: what to do, and what the planner expects to follow from it.

    The battery pair is the instruction. The ``expected_*`` fields are the solve's own view of the
    hour and are *not* recomputed here -- they are read from the columns M8 records, so a house and
    the warehouse cannot come to different conclusions about one plan.
    """

    ts_utc: str
    battery_charge_kwh: float | None
    battery_discharge_kwh: float | None
    expected_grid_import_kwh: float | None
    expected_grid_export_kwh: float | None
    expected_soc_kwh: float | None
    expected_pv_production_kwh: float | None
    expected_household_load_kwh: float | None
    import_price_ct_kwh: float | None


@dataclass(frozen=True, slots=True)
class PlanCoverage:
    """Which hours the plan covers, and how many of them it actually resolved.

    ``expected_hours`` comes from the Berlin calendar, never from a count of the rows below -- an
    expectation derived from the data under test shrinks with it, and a plan that silently covered
    nineteen hours would look complete. This is the same rule the warehouse's coverage tests apply.
    """

    local_date: str
    start_ts_utc: str
    end_ts_utc: str
    expected_hours: int
    planned_hours: int


@dataclass(frozen=True, slots=True)
class PlanProvenance:
    """Enough to tie a message on a broker back to the run that produced it.

    Every field is copied from a column ``derived.forward_dispatch_runs`` already carries -- none of
    it is minted here. ``input_digest`` is that table's identity token (M8 records it precisely
    because a re-run is *not* guaranteed to reproduce a byte-identical schedule, so a divergence has
    to be attributable rather than merely noticed); the model keys and rule ids are the same ones
    M7 stamps on every prediction row.
    """

    input_digest: str | None
    pv_model_key: str | None
    load_model_key: str | None
    decision_rule_id: str | None
    price_publication_rule_id: str | None
    selection_rule_id: str | None
    training_data_source: str | None
    solver: str | None
    solver_version: str | None


@dataclass(frozen=True, slots=True)
class PlanPayload:
    """The whole published document.

    ``published_at`` is deliberately the only field that changes when nothing else has: it is
    excluded from :meth:`digest`, so re-publishing an unchanged plan is recognisably a no-op rather
    than a new message every time the schedule fires.
    """

    schema_version: int
    kind: str
    advisory: bool
    site_id: str
    tariff_id: str
    scenario: str
    issued_at: str
    published_at: str
    plan_status: str
    coverage: PlanCoverage
    hours: tuple[PlanHour, ...]
    provenance: PlanProvenance

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        """Canonical JSON: sorted keys and tight separators, so the bytes are stable."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        """Content hash of everything except when it was sent.

        The one subtlety in the whole idempotency story. Hash the payload as-is and every run
        differs, because ``published_at`` moved -- so the ledger would record a change on every
        schedule tick and "republishing an unchanged plan is a no-op" would be false while looking
        implemented. Excluding it is what makes the claim true.
        """
        body = self.to_dict()
        body.pop("published_at")
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_topic(prefix: str, site_id: str) -> str:
    """The retained topic carrying the plan document.

    The version is in the path as well as the body: a consumer subscribes to a shape, and a
    breaking change should leave its subscription alone rather than start feeding it something it
    cannot parse.
    """
    return f"{prefix}/{site_id}/plan/v{SCHEMA_VERSION}"


def build_payload(
    *,
    site_id: str,
    tariff_id: str,
    issued_at: datetime,
    published_at: datetime,
    plan_status: str,
    coverage: PlanCoverage,
    hours: tuple[PlanHour, ...],
    provenance: PlanProvenance,
) -> PlanPayload:
    """Assemble a payload. The constant fields are set here so no caller can forget one."""
    return PlanPayload(
        schema_version=SCHEMA_VERSION,
        kind=KIND_RECOMMENDATION,
        advisory=True,
        site_id=site_id,
        tariff_id=tariff_id,
        scenario=PUBLISHED_SCENARIO,
        issued_at=_iso(issued_at),
        published_at=_iso(published_at),
        plan_status=plan_status,
        coverage=coverage,
        hours=hours,
        provenance=provenance,
    )


def _iso(moment: datetime) -> str:
    """UTC, seconds precision, ``Z``-suffixed -- the shape Home Assistant templates expect."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_instant(moment: datetime) -> str:
    """Public spelling of :func:`_iso`, for the reader building hour timestamps."""
    return _iso(moment)


def iso_date(day: date) -> str:
    return day.isoformat()
