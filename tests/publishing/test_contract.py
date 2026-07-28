"""The payload contract: what every message promises, and what it refuses to imply.

These are unit tests over pure functions -- no broker, no database. The wire-level claim (that a
retained message actually survives a reconnect) is the one thing a fake cannot make, and it lives
in ``test_broker_integration.py`` against a real Mosquitto.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from energy_platform.publishing.contract import (
    KIND_RECOMMENDATION,
    SCHEMA_VERSION,
    PlanCoverage,
    PlanHour,
    PlanPayload,
    PlanProvenance,
    build_payload,
    plan_topic,
)

ISSUED = datetime(2024, 6, 11, 22, 0, tzinfo=UTC)
PUBLISHED = datetime(2024, 6, 12, 0, 5, tzinfo=UTC)


def _hour(ts: str, charge: float | None = 0.0) -> PlanHour:
    return PlanHour(
        ts_utc=ts,
        battery_charge_kwh=charge,
        battery_discharge_kwh=0.0 if charge is not None else None,
        expected_grid_import_kwh=1.0 if charge is not None else None,
        expected_grid_export_kwh=0.0 if charge is not None else None,
        expected_soc_kwh=7.0 if charge is not None else None,
        expected_pv_production_kwh=0.0 if charge is not None else None,
        expected_household_load_kwh=1.0 if charge is not None else None,
        import_price_ct_kwh=30.0,
    )


def _payload(hours: tuple[PlanHour, ...] | None = None, **overrides: object) -> PlanPayload:
    hours = hours or (_hour("2024-06-11T22:00:00Z"), _hour("2024-06-11T23:00:00Z"))
    kwargs = {
        "site_id": "home",
        "tariff_id": "dynamic_2024",
        "issued_at": ISSUED,
        "published_at": PUBLISHED,
        "plan_status": "planned",
        "coverage": PlanCoverage(
            local_date="2024-06-12",
            start_ts_utc=hours[0].ts_utc,
            end_ts_utc=hours[-1].ts_utc,
            expected_hours=24,
            planned_hours=len([h for h in hours if h.battery_charge_kwh is not None]),
        ),
        "hours": hours,
        "provenance": PlanProvenance(
            input_digest="abc123",
            pv_model_key="pv_v1",
            load_model_key="load_v1",
            decision_rule_id="berlin_midnight_before_target_day",
            price_publication_rule_id="day_ahead_auction_d_minus_1_1245_berlin",
            selection_rule_id="latest_vintage",
            training_data_source="synthetic",
            solver="HiGHS",
            solver_version="1.7",
        ),
    }
    kwargs.update(overrides)
    return build_payload(**kwargs)  # type: ignore[arg-type]


def test_every_message_says_it_is_a_recommendation() -> None:
    """The whole point of the milestone, asserted rather than documented.

    A consumer's parser reads fields, not docstrings. If this ever stops being in the payload, a
    downstream automation has no in-band way to know it is acting on advice.
    """
    body = json.loads(_payload().to_json())
    assert body["kind"] == KIND_RECOMMENDATION
    assert body["advisory"] is True
    # And there is nothing that could be read as an instruction to a device.
    assert "command" not in body
    assert "set" not in body


def test_the_version_is_in_both_the_body_and_the_topic() -> None:
    # A consumer pins a topic; a breaking change must not mutate under a running subscription.
    assert _payload().schema_version == SCHEMA_VERSION
    assert plan_topic("energy", "home") == f"energy/home/plan/v{SCHEMA_VERSION}"


def test_an_unplanned_hour_publishes_null_and_never_zero() -> None:
    """Zero means hold, which is an instruction. Absence must not be spelled as one.

    This is the trap the reader fell into on a fallback day: the warehouse legitimately stores a
    0.0 hold for an hour the solve could not resolve, and publishing that verbatim told a house to
    do something specific on an hour nobody planned.
    """
    hours = (_hour("2024-06-11T22:00:00Z"), _hour("2024-06-11T23:00:00Z", charge=None))
    body = json.loads(_payload(hours).to_json())
    assert body["hours"][1]["battery_charge_kwh"] is None
    assert body["hours"][1]["expected_soc_kwh"] is None
    # ...and the coverage count agrees with the nulls rather than with the row count.
    assert body["coverage"]["planned_hours"] == 1
    assert body["coverage"]["expected_hours"] == 24


def test_the_digest_ignores_when_it_was_sent() -> None:
    """The single subtlety that makes 'republishing is a no-op' true rather than merely claimed.

    Hash the payload as-is and every run differs because `published_at` moved -- the ledger would
    record a change on every schedule tick while the plan had not changed at all.
    """
    early = _payload(published_at=PUBLISHED)
    later = _payload(published_at=datetime(2024, 6, 12, 6, 30, tzinfo=UTC))
    assert early.published_at != later.published_at
    assert early.digest() == later.digest()


def test_the_digest_notices_a_changed_instruction() -> None:
    # The other half: a digest that ignored everything would also be "stable".
    changed = _payload((_hour("2024-06-11T22:00:00Z", charge=2.5), _hour("2024-06-11T23:00:00Z")))
    assert changed.digest() != _payload().digest()


def test_the_json_is_canonical_so_bytes_are_stable() -> None:
    # Two payloads built the same way must serialise identically, or the digest is decorative.
    assert _payload().to_json() == _payload().to_json()
    assert json.loads(_payload().to_json())["hours"][0]["ts_utc"] == "2024-06-11T22:00:00Z"


def test_timestamps_are_utc_with_a_z_suffix() -> None:
    # Home Assistant's `device_class: timestamp` needs an unambiguous instant; a naive local
    # string would be silently reinterpreted in the viewer's zone.
    body = json.loads(_payload().to_json())
    assert body["issued_at"] == "2024-06-11T22:00:00Z"
    assert body["published_at"].endswith("Z")


def test_provenance_carries_the_tokens_the_warehouse_already_stores() -> None:
    # So a message can be traced back to the run behind it. Minting a new id here would produce a
    # token that matches nothing in derived.
    provenance = json.loads(_payload().to_json())["provenance"]
    assert provenance["input_digest"] == "abc123"
    assert provenance["decision_rule_id"] == "berlin_midnight_before_target_day"
    assert provenance["solver"] == "HiGHS"


@pytest.mark.parametrize("prefix", ["energy", "home/energy"])
def test_the_topic_prefix_is_configurable(prefix: str) -> None:
    assert plan_topic(prefix, "home").startswith(f"{prefix}/home/")
