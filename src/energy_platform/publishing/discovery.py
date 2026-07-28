"""Home Assistant MQTT discovery, for the scalars a plan can honestly be reduced to.

**Discovery is built for scalar sensors, and a plan is a document.** Home Assistant's MQTT
discovery registers entities that hold *a* value -- a number, a timestamp, a string. A day-ahead
schedule is twenty-four rows. Forcing the schedule through a sensor schema would produce an entity
whose state is a JSON blob truncated at Home Assistant's 255-character state limit, which is worse
than not registering it at all.

So the split is: the **document** goes to one retained topic and stays a document, and discovery
registers a small set of **heads** -- the scalars a dashboard or an automation would actually bind
to. Each head's ``state_topic`` points at that same document, and a ``value_template`` extracts its
field. One source of truth on the wire, N entities in Home Assistant, no second publish path that
could disagree with the first.

The heads deliberately stop at what is unambiguous. There is no "should I charge right now" entity,
because that would be a control signal wearing a sensor's clothes, and this platform publishes
recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from energy_platform.publishing.contract import SCHEMA_VERSION

# The device every head is grouped under, so Home Assistant shows one "Energy Platform" device
# rather than four loose sensors. Keyed by site, so two sites do not collide.
DEVICE_MANUFACTURER: Final = "energy-platform"


@dataclass(frozen=True, slots=True)
class DiscoveryHead:
    """One scalar entity extracted from the plan document."""

    key: str
    name: str
    value_template: str
    device_class: str | None = None
    unit: str | None = None
    icon: str | None = None


# `now()` in a value_template is evaluated by Home Assistant, not here: the "next hour" entity has
# to track the clock, and a value baked in at publish time would be stale within the hour. The
# selectattr walk finds the first hour at or after the current one and reports its net battery
# instruction -- positive charge, negative discharge -- as a single signed number, which is the
# shape an automation or a gauge can actually use.
_NEXT_HOUR_TEMPLATE: Final = (
    # `utcnow()` is Home Assistant's own template function, not Python's -- the payload's
    # timestamps are UTC with a Z suffix, so the comparison has to be made in UTC or the entity
    # would read the wrong hour by however far Berlin is from it (one or two, seasonally).
    "{% set t = utcnow().strftime('%Y-%m-%dT%H:00:00Z') %}"
    "{% set future = value_json.hours "
    "| selectattr('ts_utc', 'ge', t) "
    "| selectattr('battery_charge_kwh', 'ne', None) | list %}"
    "{% if future %}"
    "{{ (future[0].battery_charge_kwh - future[0].battery_discharge_kwh) | round(3) }}"
    "{% else %}unknown{% endif %}"
)

HEADS: Final[tuple[DiscoveryHead, ...]] = (
    DiscoveryHead(
        key="next_hour_battery_kwh",
        name="Recommended battery energy, next hour",
        value_template=_NEXT_HOUR_TEMPLATE,
        unit="kWh",
        icon="mdi:battery-clock",
    ),
    DiscoveryHead(
        key="plan_issued_at",
        name="Plan issued at",
        value_template="{{ value_json.issued_at }}",
        device_class="timestamp",
    ),
    DiscoveryHead(
        key="plan_date",
        name="Plan date",
        value_template="{{ value_json.coverage.local_date }}",
        icon="mdi:calendar",
    ),
    DiscoveryHead(
        key="plan_status",
        name="Plan status",
        value_template="{{ value_json.plan_status }}",
        icon="mdi:information-outline",
    ),
    DiscoveryHead(
        key="plan_hours",
        name="Planned hours",
        value_template="{{ value_json.coverage.planned_hours }}",
        icon="mdi:clock-outline",
    ),
)


def discovery_topic(discovery_prefix: str, site_id: str, key: str) -> str:
    return f"{discovery_prefix}/sensor/{_object_id(site_id, key)}/config"


def discovery_config(head: DiscoveryHead, *, site_id: str, state_topic: str) -> dict[str, Any]:
    """One discovery message.

    ``unique_id`` is stable across republishes so Home Assistant updates the entity instead of
    creating a duplicate every time the schedule fires, and every head advertises the same
    ``state_topic`` -- the plan document -- rather than a private copy of its own value.
    """
    config: dict[str, Any] = {
        "name": head.name,
        "unique_id": _object_id(site_id, head.key),
        "object_id": _object_id(site_id, head.key),
        "state_topic": state_topic,
        "value_template": head.value_template,
        "device": {
            "identifiers": [f"{DEVICE_MANUFACTURER}_{site_id}"],
            "name": f"Energy Platform ({site_id})",
            "manufacturer": DEVICE_MANUFACTURER,
            "model": f"dispatch plan v{SCHEMA_VERSION}",
        },
    }
    if head.device_class is not None:
        config["device_class"] = head.device_class
    if head.unit is not None:
        config["unit_of_measurement"] = head.unit
    if head.icon is not None:
        config["icon"] = head.icon
    return config


def _object_id(site_id: str, key: str) -> str:
    return f"{DEVICE_MANUFACTURER.replace('-', '_')}_{site_id}_{key}"
