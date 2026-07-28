"""The publish decision, against an in-process broker and a fake reader.

Everything here runs with no broker and no database: the publisher takes an injected
:class:`~energy_platform.publishing.client.MqttPublisher` and an optional ledger, so the whole
decision path -- what goes on the wire, in what order, retained or not, and whether to send at all
-- is assertable in milliseconds.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from energy_platform.publishing.client import Message, RecordingPublisher
from energy_platform.publishing.contract import (
    PlanHour,
    PlanProvenance,
    plan_topic,
)
from energy_platform.publishing.discovery import HEADS
from energy_platform.publishing.publisher import PublishOutcome, publish_plan
from energy_platform.publishing.reader import ForwardPlan, PlanNotAvailableError

DAY = date(2024, 6, 12)
NOW = datetime(2024, 6, 12, 0, 5, tzinfo=UTC)
DECISION = datetime(2024, 6, 11, 22, 0, tzinfo=UTC)


def _hour(ts: str, charge: float = 0.0) -> PlanHour:
    return PlanHour(
        ts_utc=ts,
        battery_charge_kwh=charge,
        battery_discharge_kwh=0.0,
        expected_grid_import_kwh=1.0,
        expected_grid_export_kwh=0.0,
        expected_soc_kwh=7.0,
        expected_pv_production_kwh=0.0,
        expected_household_load_kwh=1.0,
        import_price_ct_kwh=30.0,
    )


class _StubReader:
    """A ``ForwardPlanReader`` stand-in, in the M3 stub tradition. Serves one plan, or raises."""

    def __init__(self, plan: ForwardPlan | None = None, error: str | None = None) -> None:
        self._plan = plan
        self._error = error

    def read_day(self, site_id: str, day: date, tariff_id: str) -> ForwardPlan:
        if self._error is not None:
            raise PlanNotAvailableError(self._error)
        assert self._plan is not None
        return self._plan


class _RecordingLedger:
    """The ledger's read/write surface, in memory."""

    def __init__(self, seeded: str | None = None) -> None:
        self.seeded = seeded
        self.records: list[object] = []

    def last_digest(self, site_id: str, day: date, tariff_id: str, topic: str) -> str | None:
        return self.seeded

    def record(self, publication: object) -> None:
        self.records.append(publication)


def _plan(hours: tuple[PlanHour, ...] | None = None, status: str = "planned") -> ForwardPlan:
    hours = hours or (_hour("2024-06-11T22:00:00Z"), _hour("2024-06-11T23:00:00Z"))
    return ForwardPlan(
        site_id="home",
        tariff_id="dynamic_2024",
        local_date=DAY,
        plan_status=status,
        decision_time=DECISION,
        hours=hours,
        provenance=PlanProvenance(
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
    )


def _publish(
    reader: _StubReader,
    publisher: RecordingPublisher,
    ledger: _RecordingLedger | None,
    *,
    discovery_prefix: str | None = "homeassistant",
    retain: bool = True,
) -> PublishOutcome:
    return publish_plan(
        reader,  # type: ignore[arg-type]
        publisher,
        ledger,  # type: ignore[arg-type]
        site_id="home",
        day=DAY,
        tariff_id="dynamic_2024",
        now=NOW,
        topic_prefix="energy",
        qos=1,
        retain=retain,
        discovery_prefix=discovery_prefix,
    )


def test_the_plan_goes_to_one_retained_topic() -> None:
    publisher = RecordingPublisher()
    outcome = _publish(_StubReader(_plan()), publisher, _RecordingLedger())

    assert outcome.published
    plan_message = publisher.messages[0]
    assert plan_message.topic == plan_topic("energy", "home")
    assert plan_message.retain is True, "retention is what lets HA re-read the plan after a restart"
    assert plan_message.qos == 1
    assert json.loads(plan_message.payload)["coverage"]["local_date"] == "2024-06-12"


def test_the_plan_document_is_published_before_any_discovery_config() -> None:
    """Ordering matters on a partial failure.

    A house holding the plan but no entities is strictly better off than one holding entities that
    point at a topic with nothing on it -- the second renders as a row of "unavailable" sensors.
    """
    publisher = RecordingPublisher()
    _publish(_StubReader(_plan()), publisher, _RecordingLedger())
    assert publisher.messages[0].topic.startswith("energy/")
    assert all(m.topic.startswith("homeassistant/") for m in publisher.messages[1:])


def test_discovery_registers_scalars_that_read_from_the_plan_topic() -> None:
    """One source of truth on the wire: heads extract from the document, never duplicate it."""
    publisher = RecordingPublisher()
    _publish(_StubReader(_plan()), publisher, _RecordingLedger())

    configs = [json.loads(m.payload) for m in publisher.messages[1:]]
    assert len(configs) == len(HEADS)
    for config in configs:
        assert config["state_topic"] == plan_topic("energy", "home")
        assert "value_template" in config
        # Stable across republishes, or HA creates a duplicate entity every time we publish.
        assert config["unique_id"].startswith("energy_platform_home_")
        assert config["device"]["identifiers"] == ["energy-platform_home"]


def test_discovery_configs_are_retained_too() -> None:
    # HA re-reads discovery on restart; an unretained config leaves the entities unregistered.
    publisher = RecordingPublisher()
    _publish(_StubReader(_plan()), publisher, _RecordingLedger())
    assert all(message.retain for message in publisher.messages)


def test_discovery_can_be_switched_off_without_losing_the_plan() -> None:
    # The document is the contract; the entities are a convenience layered on it.
    publisher = RecordingPublisher()
    _publish(_StubReader(_plan()), publisher, _RecordingLedger(), discovery_prefix=None)
    assert len(publisher.messages) == 1


def test_republishing_an_unchanged_plan_sends_nothing() -> None:
    """The idempotency claim, exercised end to end rather than asserted about the digest.

    Retention already means a resend would overwrite rather than duplicate; this is about the
    ledger not reporting a publication that changed nothing.
    """
    first_publisher = RecordingPublisher()
    ledger = _RecordingLedger()
    first = _publish(_StubReader(_plan()), first_publisher, ledger)
    assert first.published

    # Second run, same plan, ledger primed with what the first wrote.
    second_publisher = RecordingPublisher()
    primed = _RecordingLedger(seeded=first.payload.digest())
    second = _publish(_StubReader(_plan()), second_publisher, primed)

    assert not second.published
    assert second_publisher.messages == []
    assert primed.records == [], "an unchanged plan must not touch the ledger either"
    assert "unchanged" in second.reason


def test_a_changed_plan_is_republished_and_says_so() -> None:
    publisher = RecordingPublisher()
    ledger = _RecordingLedger(seeded="a-digest-from-an-older-plan")
    outcome = _publish(_StubReader(_plan()), publisher, ledger)

    assert outcome.published
    assert "changed" in outcome.reason
    assert len(ledger.records) == 1


def test_a_dry_run_publishes_and_records_nothing() -> None:
    # ledger=None is the --dry-run path: it must still assemble a real payload.
    publisher = RecordingPublisher()
    outcome = _publish(_StubReader(_plan()), publisher, None)
    assert outcome.published
    assert outcome.payload.coverage.local_date == "2024-06-12"


def test_a_missing_plan_refuses_instead_of_publishing_an_empty_one() -> None:
    """The failure mode that matters most: silence is safer than a plan for no hours.

    An empty retained document would sit on the broker looking authoritative, and a house acting
    on it would see zero recommended everything.
    """
    publisher = RecordingPublisher()
    reader = _StubReader(error="no simulated forecast_driven plan for site 'home' on 2024-06-12")
    with pytest.raises(PlanNotAvailableError, match="no simulated"):
        _publish(reader, publisher, _RecordingLedger())
    assert publisher.messages == []


def test_a_fallback_day_is_still_published_and_labelled() -> None:
    """A day the planner fell back on is real advice, and the house should get it.

    Suppressing it would leave yesterday's retained plan in place, which is worse: the house would
    act on a stale schedule believing it current. The status travels so a consumer can tell.
    """
    publisher = RecordingPublisher()
    outcome = _publish(_StubReader(_plan(status="fallback_naive")), publisher, _RecordingLedger())
    assert outcome.published
    assert json.loads(publisher.messages[0].payload)["plan_status"] == "fallback_naive"


def test_retain_off_is_honoured_even_though_it_is_a_bad_idea() -> None:
    # Config is config. The default is on and the docs say why; a deployment that turns it off
    # gets what it asked for rather than a silent override.
    publisher = RecordingPublisher()
    _publish(_StubReader(_plan()), publisher, _RecordingLedger(), retain=False)
    assert all(not message.retain for message in publisher.messages)


def test_the_recording_publisher_satisfies_the_protocol() -> None:
    # Positive control on the injection point itself: if RecordingPublisher drifted from the
    # Protocol, every test above would be exercising something the real path cannot use.
    from energy_platform.publishing.client import MqttPublisher

    assert isinstance(RecordingPublisher(), MqttPublisher)
    assert isinstance(Message("t", "p", 1, True), Message)
