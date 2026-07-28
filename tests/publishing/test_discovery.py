"""Home Assistant discovery: the entity configs, and the templates nothing else would check.

The ``value_template`` strings are rendered by **Home Assistant**, not by this codebase. Nothing in
the runtime path parses them, so a broken one publishes perfectly happily and surfaces as a row of
``unknown`` sensors in somebody's house -- days later, with no error anywhere. Rendering them here
against the payload shape they are written for is the only place that can go wrong loudly.
"""

from __future__ import annotations

import datetime
import json

import pytest
from jinja2 import Environment

from energy_platform.publishing.contract import plan_topic
from energy_platform.publishing.discovery import HEADS, discovery_config, discovery_topic

STATE_TOPIC = plan_topic("energy", "home")

# A day with a hole in it, because that is the shape most likely to break a template: the hour the
# planner could not resolve carries nulls, and a template that ignored them would report a hold.
DOCUMENT = {
    "issued_at": "2024-06-11T22:00:00Z",
    "plan_status": "planned",
    "coverage": {"local_date": "2024-06-12", "planned_hours": 23, "expected_hours": 24},
    "hours": [
        {"ts_utc": "2024-06-12T09:00:00Z", "battery_charge_kwh": 0.0, "battery_discharge_kwh": 1.0},
        {
            "ts_utc": "2024-06-12T10:00:00Z",
            "battery_charge_kwh": None,
            "battery_discharge_kwh": None,
        },
        {"ts_utc": "2024-06-12T11:00:00Z", "battery_charge_kwh": 2.5, "battery_discharge_kwh": 0.0},
    ],
}


def _render(template: str, at: datetime.datetime) -> str:
    """Render one template the way Home Assistant would, with its `utcnow` in scope."""
    return Environment().from_string(template).render(value_json=DOCUMENT, utcnow=lambda: at)


def _head(key: str) -> str:
    return next(head.value_template for head in HEADS if head.key == key)


def test_every_template_renders_against_a_real_payload() -> None:
    at = datetime.datetime(2024, 6, 12, 10, 30, tzinfo=datetime.UTC)
    for head in HEADS:
        rendered = _render(head.value_template, at)
        assert rendered != "", f"{head.key} rendered empty"


def test_the_next_hour_entity_skips_an_unplanned_hour() -> None:
    """At 10:30 the next hour is 11:00, because 10:00 was never planned.

    A template that selected purely on time would land on the null hour and report a hold -- the
    same null-versus-zero conflation the payload contract exists to prevent, reintroduced one layer
    further out where no test in the Python would see it.
    """
    at = datetime.datetime(2024, 6, 12, 10, 30, tzinfo=datetime.UTC)
    assert _render(_head("next_hour_battery_kwh"), at) == "2.5"


def test_the_next_hour_entity_is_signed_charge_positive() -> None:
    # At 08:30 the next hour is 09:00, which discharges 1.0 -> -1.0. One signed number is what an
    # automation or a gauge can act on; two columns would need the consumer to do the arithmetic.
    at = datetime.datetime(2024, 6, 12, 8, 30, tzinfo=datetime.UTC)
    assert _render(_head("next_hour_battery_kwh"), at) == "-1.0"


def test_the_next_hour_entity_degrades_to_unknown_past_the_end_of_the_plan() -> None:
    # After the last planned hour there is no recommendation, and "unknown" is Home Assistant's
    # word for that. Reporting 0.0 would tell the house to hold, forever, on stale data.
    at = datetime.datetime(2024, 6, 13, 6, 0, tzinfo=datetime.UTC)
    assert _render(_head("next_hour_battery_kwh"), at) == "unknown"


def test_scalar_heads_read_straight_from_the_document() -> None:
    at = datetime.datetime(2024, 6, 12, 10, 30, tzinfo=datetime.UTC)
    assert _render(_head("plan_issued_at"), at) == "2024-06-11T22:00:00Z"
    assert _render(_head("plan_date"), at) == "2024-06-12"
    assert _render(_head("plan_status"), at) == "planned"
    assert _render(_head("plan_hours"), at) == "23"


def test_every_head_points_at_the_plan_topic_rather_than_its_own() -> None:
    """One source of truth on the wire.

    A second state topic per entity would be a copy of the plan free to disagree with it, and
    would multiply the retained messages a consumer has to reason about by six.
    """
    for head in HEADS:
        config = discovery_config(head, site_id="home", state_topic=STATE_TOPIC)
        assert config["state_topic"] == STATE_TOPIC


def test_unique_ids_are_stable_and_site_scoped() -> None:
    # Stable, or Home Assistant creates a duplicate entity on every republish. Site-scoped, or two
    # sites publishing to one broker collide on entity ids.
    home = {
        json.dumps(discovery_config(h, site_id="home", state_topic=STATE_TOPIC)["unique_id"])
        for h in HEADS
    }
    other = {
        json.dumps(discovery_config(h, site_id="cabin", state_topic=STATE_TOPIC)["unique_id"])
        for h in HEADS
    }
    assert len(home) == len(HEADS)
    assert home.isdisjoint(other)


def test_the_energy_head_declares_its_unit() -> None:
    # Without a unit HA treats the state as a string and neither graphs nor sums it.
    config = discovery_config(
        next(h for h in HEADS if h.key == "next_hour_battery_kwh"),
        site_id="home",
        state_topic=STATE_TOPIC,
    )
    assert config["unit_of_measurement"] == "kWh"


def test_no_head_registers_anything_that_could_actuate() -> None:
    """Discovery registers *sensors*. A switch or a number would be a control surface.

    This platform publishes recommendations; the moment discovery advertised a writable entity,
    Home Assistant would offer a UI control that looked like it did something here.
    """
    for head in HEADS:
        assert discovery_topic("homeassistant", "home", head.key).startswith(
            "homeassistant/sensor/"
        )
        config = discovery_config(head, site_id="home", state_topic=STATE_TOPIC)
        assert "command_topic" not in config


@pytest.mark.parametrize("head", HEADS, ids=lambda h: h.key)
def test_configs_are_json_serialisable(head: object) -> None:
    json.dumps(discovery_config(head, site_id="home", state_topic=STATE_TOPIC))  # type: ignore[arg-type]
