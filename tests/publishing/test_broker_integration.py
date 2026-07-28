"""The one test that needs a real broker, and the one claim a fake cannot make.

Everything else about publishing is asserted in-process against ``RecordingPublisher``: what goes
on the wire, in what order, whether to send at all. That covers the logic completely and costs
nothing.

What it cannot cover is the property the whole design rests on -- that a **retained** message
actually persists on a broker and is delivered to a subscriber that connects *afterwards*. A fake
asserting its own `retain=True` flag proves only that we set a boolean. Retention is a behaviour of
the broker, so verifying it requires a broker; this is the same reasoning that puts the raw-zone
idempotency tests on a real Postgres rather than a stub.

Gated on ``ENERGY_REQUIRE_MQTT``: skipped locally when no broker is running, and a hard failure in
CI, where a skip would mean the claim silently stopped being checked.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, date, datetime

import pytest

from energy_platform.config import MqttConfig
from energy_platform.publishing.client import Message, MqttPublishError, connect
from energy_platform.publishing.contract import (
    PlanCoverage,
    PlanHour,
    PlanProvenance,
    build_payload,
    plan_topic,
)

pytestmark = pytest.mark.mqtt

HOST = os.environ.get("ENERGY_MQTT_HOST", "localhost")
PORT = int(os.environ.get("ENERGY_MQTT_PORT", "1883"))
# Unique per run so a re-run on a persistent broker cannot pass on the previous run's message --
# the exact way a retention test can lie to you.
TOPIC_SITE = f"itest{os.getpid()}"


def _broker_config(**overrides: object) -> MqttConfig:
    base = {
        "enabled": True,
        "host": HOST,
        "port": PORT,
        "topic_prefix": "energy-itest",
        "timeout_seconds": 10.0,
    }
    base.update(overrides)
    return MqttConfig(**base)  # type: ignore[arg-type]


def _require_broker() -> None:
    """Skip without a broker, unless CI has declared one must be there."""
    try:
        with socket.create_connection((HOST, PORT), timeout=2):
            return
    except OSError as exc:
        message = f"no MQTT broker at {HOST}:{PORT} ({exc})"
        if os.environ.get("ENERGY_REQUIRE_MQTT"):
            pytest.fail(message)
        pytest.skip(message)


@pytest.fixture(autouse=True)
def broker() -> None:
    _require_broker()


def _payload_json() -> str:
    hours = (
        PlanHour(
            ts_utc="2024-06-11T22:00:00Z",
            battery_charge_kwh=1.25,
            battery_discharge_kwh=0.0,
            expected_grid_import_kwh=1.25,
            expected_grid_export_kwh=0.0,
            expected_soc_kwh=8.0,
            expected_pv_production_kwh=0.0,
            expected_household_load_kwh=0.0,
            import_price_ct_kwh=30.0,
        ),
    )
    return build_payload(
        site_id=TOPIC_SITE,
        tariff_id="dynamic_2024",
        issued_at=datetime(2024, 6, 11, 22, 0, tzinfo=UTC),
        published_at=datetime(2024, 6, 12, 0, 5, tzinfo=UTC),
        plan_status="planned",
        coverage=PlanCoverage(
            local_date=date(2024, 6, 12).isoformat(),
            start_ts_utc=hours[0].ts_utc,
            end_ts_utc=hours[-1].ts_utc,
            expected_hours=24,
            planned_hours=1,
        ),
        hours=hours,
        provenance=PlanProvenance(
            input_digest="itest",
            pv_model_key=None,
            load_model_key=None,
            decision_rule_id="berlin_midnight_before_target_day",
            price_publication_rule_id="day_ahead_auction_d_minus_1_1245_berlin",
            selection_rule_id="latest_vintage",
            training_data_source="synthetic",
            solver="HiGHS",
            solver_version="1.7",
        ),
    ).to_json()


def _subscribe_once(topic: str, timeout: float = 5.0) -> str | None:
    """Connect fresh, subscribe, and return the first retained payload -- or None."""
    from paho.mqtt.client import Client
    from paho.mqtt.enums import CallbackAPIVersion

    received: list[str] = []
    client = Client(CallbackAPIVersion.VERSION2, client_id=f"{TOPIC_SITE}-sub")

    def on_message(_client: object, _userdata: object, message: object) -> None:
        received.append(message.payload.decode())  # type: ignore[attr-defined]

    client.on_message = on_message
    client.connect(HOST, PORT, keepalive=10)
    client.subscribe(topic, qos=1)
    client.loop_start()
    deadline = timeout
    step = 0.1
    while not received and deadline > 0:
        import time

        time.sleep(step)
        deadline -= step
    client.loop_stop()
    client.disconnect()
    return received[0] if received else None


def test_a_retained_plan_reaches_a_subscriber_that_connects_afterwards() -> None:
    """The claim the whole design rests on, verified against a real broker.

    Publish, disconnect entirely, then connect a *new* subscriber. If retention were not honoured
    the subscriber would see nothing -- which is exactly what a Home Assistant instance restarting
    at 3am would see, and precisely the failure a fake cannot detect.
    """
    topic = plan_topic("energy-itest", TOPIC_SITE)
    payload = _payload_json()

    with connect(_broker_config()) as publisher:
        publisher.publish(Message(topic=topic, payload=payload, qos=1, retain=True))

    # The publishing connection is now closed. A fresh subscriber must still get the plan.
    received = _subscribe_once(topic)
    assert received is not None, "a retained plan was not delivered to a later subscriber"
    assert json.loads(received)["site_id"] == TOPIC_SITE
    assert json.loads(received)["kind"] == "recommendation"


def test_republishing_overwrites_rather_than_appending() -> None:
    """Retention is what makes 'never a duplicate stream' structural rather than enforced.

    A retained topic holds exactly one message. Publish twice and a new subscriber sees the second
    one only -- so even if the ledger's no-op check were bypassed entirely, the house would never
    accumulate a backlog of plans.
    """
    topic = plan_topic("energy-itest", f"{TOPIC_SITE}-overwrite")
    with connect(_broker_config()) as publisher:
        publisher.publish(Message(topic, json.dumps({"generation": 1}), qos=1, retain=True))
        publisher.publish(Message(topic, json.dumps({"generation": 2}), qos=1, retain=True))

    received = _subscribe_once(topic)
    assert received is not None
    assert json.loads(received)["generation"] == 2


def test_an_unroutable_broker_fails_loudly_rather_than_hanging() -> None:
    # 192.0.2.0/24 is TEST-NET-1: guaranteed unroutable, so this exercises the connect failure
    # path without depending on a host that might exist.
    config = _broker_config(host="192.0.2.1", timeout_seconds=1.0)
    with pytest.raises(MqttPublishError, match="could not reach"), connect(config):
        pass
